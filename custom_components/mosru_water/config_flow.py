"""Config flow для mosru_water."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from homeassistant.components.persistent_notification import (
    async_create as pn_create,
    async_dismiss as pn_dismiss,
)

from .api import MosRuAuthError, MosRuApiError, MosRuClient
from .const import (
    DOMAIN,
    CONF_PAYCODE, CONF_FLAT,
    CONF_COLD_ID, CONF_HOT_ID,
    CONF_COLD_ENTITY, CONF_HOT_ENTITY, CONF_SUBMIT_DAY,
    CONF_SESSION_COOKIES,
    UNIT_M3,
)

_LOGGER = logging.getLogger(__name__)

_QR_FILE = "mosru_water_qr.svg"
_QR_POLL_SECONDS = 150  # максимальное время ожидания сканирования


def _validate_sensor(hass: HomeAssistant, entity_id: str) -> str | None:
    """Проверить что сенсор существует и возвращает м³. Вернуть ключ ошибки или None."""
    state = hass.states.get(entity_id)
    if state is None:
        return "entity_not_found"
    if state.attributes.get("unit_of_measurement", "") != UNIT_M3:
        return "wrong_unit"
    return None


def _write_qr_svg(www_dir: str, link: str, cache_buster: int) -> str:
    """Записать QR-код как SVG файл. Возвращает /local/ URL."""
    try:
        import qrcode
        import qrcode.image.svg

        os.makedirs(www_dir, exist_ok=True)
        qr_path = os.path.join(www_dir, _QR_FILE)
        factory = qrcode.image.svg.SvgFillImage
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        qr_img = qr.make_image(image_factory=factory)
        with open(qr_path, "wb") as f:
            qr_img.save(f)
        return f"/local/{_QR_FILE}?t={cache_buster}"
    except Exception:
        _LOGGER.exception("Не удалось сгенерировать QR-код")
        return ""


class MosRuWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Мастер настройки интеграции MOS.RU Water Meter."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._counters: list[dict] = []
        self._counters_fetched: bool = False
        self._client: MosRuClient | None = None
        self._qr_task: asyncio.Task | None = None
        self._qr_url: str = ""
        self._qr_link: str = ""
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ── Шаг 1: код плательщика и квартира ────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ввод кода плательщика и номера квартиры."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            self._data.update(user_input)
            self._client = MosRuClient()
            return await self.async_step_qr()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PAYCODE): str,
                vol.Required(CONF_FLAT):    str,
            }),
        )

    # ── Шаг 2: QR-авторизация ────────────────────────────────────────────

    async def async_step_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Показать QR-код и ждать сканирования."""
        if self._qr_task is None:
            try:
                qr_data = await self.hass.async_add_executor_job(
                    self._client.start_qr_session
                )
            except MosRuApiError:
                return self.async_abort(reason="cannot_connect")

            ts = int(time.time())
            www_dir = self.hass.config.path("www")
            self._qr_link = qr_data["link"]
            self._qr_url = await self.hass.async_add_executor_job(
                _write_qr_svg, www_dir, self._qr_link, ts
            )
            self._qr_task = self.hass.async_create_task(self._poll_qr_scan())
            pn_create(
                self.hass,
                message=(
                    f"Отсканируйте QR-код приложением **mos.ru** "
                    f"или **Госуслуги Москвы**:\n\n"
                    f"![QR-код]({self._qr_url})\n\n"
                    f"[Открыть QR-код в новой вкладке]({self._qr_link})"
                ),
                title="MOS.RU Water: Авторизация",
                notification_id="mosru_water_qr",
            )

        if not self._qr_task.done():
            return self.async_show_progress(
                step_id="qr",
                progress_action="scanning",
                progress_task=self._qr_task,
                description_placeholders={"qr_url": self._qr_url},
            )

        # Задача завершена
        try:
            result = self._qr_task.result()
        except Exception:
            self._qr_task = None
            return self.async_abort(reason="cannot_connect")

        self._qr_task = None

        if result == "code_required":
            pn_dismiss(self.hass, notification_id="mosru_water_qr")
            return self.async_show_progress_done(next_step_id="code")

        if not result:
            # QR истёк или ошибка — начать заново со свежей сессией
            self._client = MosRuClient()
            return await self.async_step_qr()

        pn_dismiss(self.hass, notification_id="mosru_water_qr")

        # Сохранить cookies
        cookies = await self.hass.async_add_executor_job(
            self._client.get_session_cookies
        )
        self._data[CONF_SESSION_COOKIES] = cookies

        if self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={**self._reauth_entry.data, CONF_SESSION_COOKIES: cookies},
            )
            return self.async_abort(reason="reauth_successful")

        return self.async_show_progress_done(next_step_id="discover")

    async def _poll_qr_scan(self) -> bool:
        """Фоновая задача: опросить QR до сканирования или истечения."""
        _LOGGER.debug("QR polling started")
        for tick in range(_QR_POLL_SECONDS):
            await asyncio.sleep(1)
            try:
                command = await self.hass.async_add_executor_job(self._client.poll_qr)
            except MosRuApiError as err:
                _LOGGER.error("QR poll error at tick %d: %s", tick, err)
                return False

            if tick % 10 == 0:
                _LOGGER.debug("QR poll tick=%d command=%r", tick, command)

            if command == "needComplete":
                try:
                    status = await self.hass.async_add_executor_job(
                        self._client.complete_qr_auth
                    )
                except (MosRuAuthError, MosRuApiError):
                    return False
                if status == "sms_required":
                    return "code_required"
                return True

            if command == "askForConfirm":
                # Сервер отправил пуш «Подтвердить вход?» на телефон.
                # Продолжаем поллинг — после тапа «Подтвердить» придёт needComplete.
                _LOGGER.debug("QR poll tick=%d askForConfirm, waiting for user to confirm", tick)
                continue

            if command == "needRefresh":
                try:
                    qr_data = await self.hass.async_add_executor_job(
                        self._client.refresh_qr
                    )
                    ts = int(time.time())
                    www_dir = self.hass.config.path("www")
                    self._qr_link = qr_data["link"]
                    self._qr_url = await self.hass.async_add_executor_job(
                        _write_qr_svg, www_dir, self._qr_link, ts
                    )
                except MosRuApiError:
                    return False
                continue

            # showQRCode — продолжаем опрос

        return False  # таймаут

    # ── Шаг 3: ввод 6-значного 2FA-кода ─────────────────────────────────

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ввод 6-значного кода из пуш-уведомления (2FA)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("sms_code", "").strip()
            try:
                await self.hass.async_add_executor_job(
                    self._client.submit_sms_and_trust, code
                )
            except MosRuAuthError:
                errors["sms_code"] = "invalid_code"
            except MosRuApiError:
                return self.async_abort(reason="cannot_connect")
            else:
                await self.hass.async_add_executor_job(self._client.warm_session)
                cookies = await self.hass.async_add_executor_job(
                    self._client.get_session_cookies
                )
                self._data[CONF_SESSION_COOKIES] = cookies

                if self._reauth_entry is not None:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={**self._reauth_entry.data, CONF_SESSION_COOKIES: cookies},
                    )
                    return self.async_abort(reason="reauth_successful")

                return await self.async_step_discover()

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({
                vol.Required("sms_code"): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT
                    )
                ),
            }),
            errors=errors,
        )

    # ── Шаг 4: выбор счётчиков ───────────────────────────────────────────

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Выбор счётчиков: автоматически из API или ручной ввод ID."""
        errors: dict[str, str] = {}

        # Однократно запрашиваем список счётчиков
        if not self._counters_fetched:
            self._counters_fetched = True
            try:
                self._counters = await self.hass.async_add_executor_job(
                    self._client.get_counters,
                    self._data[CONF_PAYCODE],
                    self._data[CONF_FLAT],
                )
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError:
                self._counters = []  # падаем в ручной ввод

        # ── Счётчики найдены автоматически ───────────────────────────────
        if self._counters:
            counter_options = [
                selector.SelectOptionDict(
                    value=c["id"],
                    label=f"{c['name']} ({c['type']}, ID: {c['id']})",
                )
                for c in self._counters
            ]
            if user_input is not None:
                self._data[CONF_COLD_ID] = user_input[CONF_COLD_ID]
                self._data[CONF_HOT_ID]  = user_input[CONF_HOT_ID]
                return await self.async_step_sensors()

            return self.async_show_form(
                step_id="discover",
                data_schema=vol.Schema({
                    vol.Required(CONF_COLD_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=counter_options)
                    ),
                    vol.Required(CONF_HOT_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=counter_options)
                    ),
                }),
                description_placeholders={
                    "description": "Выберите счётчики из вашего личного кабинета mos.ru",
                },
            )

        # ── Счётчики не найдены — ручной ввод ────────────────────────────
        if user_input is not None:
            if user_input.get("retry_discovery"):
                self._counters_fetched = False
                self._counters = []
                return await self.async_step_discover()

            cold_id = user_input.get(CONF_COLD_ID, "").strip()
            hot_id  = user_input.get(CONF_HOT_ID, "").strip()
            if not cold_id:
                errors[CONF_COLD_ID] = "required"
            if not hot_id:
                errors[CONF_HOT_ID] = "required"
            if not errors:
                self._data[CONF_COLD_ID] = cold_id
                self._data[CONF_HOT_ID]  = hot_id
                return await self.async_step_sensors()

        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema({
                vol.Optional(CONF_COLD_ID, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_HOT_ID, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional("retry_discovery", default=False): selector.BooleanSelector(),
            }),
            description_placeholders={
                "description": (
                    "Счётчики не найдены автоматически. "
                    "Введите ID вручную (см. mos.ru → ЖКУ → номер прибора) "
                    "или нажмите «Обновить список счётчиков»."
                ),
            },
            errors=errors,
        )

    # ── Шаг 5: HA-сенсоры ────────────────────────────────────────────────

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Выбор HA-сенсоров и дня автоотправки."""
        errors: dict[str, str] = {}

        if user_input is not None:
            for field in (CONF_COLD_ENTITY, CONF_HOT_ENTITY):
                err = _validate_sensor(self.hass, user_input[field])
                if err:
                    errors[field] = err

            if not errors:
                self._data.update(user_input)
                return self.async_create_entry(
                    title="MOS.RU Water Meter",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema({
                vol.Required(CONF_COLD_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_HOT_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_SUBMIT_DAY, default=20): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=28, mode="box")
                ),
            }),
            errors=errors,
        )

    # ── Повторная авторизация ─────────────────────────────────────────────

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Запускается при истечении сессии mos.ru."""
        entry_id = self.context.get("entry_id")
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        self._data = dict(self._reauth_entry.data)
        self._client = MosRuClient()
        self._qr_task = None
        return await self.async_step_qr()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return MosRuWaterOptionsFlow(config_entry)


# ── Options flow ──────────────────────────────────────────────────────────────


class MosRuWaterOptionsFlow(config_entries.OptionsFlow):
    """Настройки после установки: изменить день отправки и сенсоры."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        data = {**self._entry.data, **self._entry.options}

        if user_input is not None:
            for field in (CONF_COLD_ENTITY, CONF_HOT_ENTITY):
                err = _validate_sensor(self.hass, user_input[field])
                if err:
                    errors[field] = err
            if not user_input.get(CONF_COLD_ID, "").strip():
                errors[CONF_COLD_ID] = "required"
            if not user_input.get(CONF_HOT_ID, "").strip():
                errors[CONF_HOT_ID] = "required"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_COLD_ID, default=data.get(CONF_COLD_ID, "")
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(
                    CONF_HOT_ID, default=data.get(CONF_HOT_ID, "")
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Required(
                    CONF_COLD_ENTITY, default=data.get(CONF_COLD_ENTITY, "")
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_HOT_ENTITY, default=data.get(CONF_HOT_ENTITY, "")
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_SUBMIT_DAY, default=int(data.get(CONF_SUBMIT_DAY, 20))
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=28, mode="box")
                ),
            }),
            errors=errors,
        )
