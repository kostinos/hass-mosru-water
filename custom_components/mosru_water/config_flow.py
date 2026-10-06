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

from .api import (
    MosRuAuthError, MosRuApiError, MosRuClient,
    COLD_TYPE, HOT_TYPE, counters_of_type, pick_counters, place_label,
)
from .const import (
    DOMAIN,
    CONF_PAYCODE, CONF_FLAT, CONF_USER_PLACE_ID,
    CONF_COLD_ID, CONF_HOT_ID,
    CONF_COLD_ENTITY, CONF_HOT_ENTITY, CONF_SUBMIT_DAY,
    CONF_SESSION_COOKIES,
    UNIT_M3,
)

_LOGGER = logging.getLogger(__name__)

_AUTH_SETTINGS_URL = "/config/integrations/integration/mosru_water"
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
        self._places: list[dict] | None = None
        self._suggested_counters: tuple[str | None, str | None] = (None, None)
        self._client: MosRuClient | None = None
        self._qr_task: asyncio.Task | None = None
        self._qr_url: str = ""
        self._qr_link: str = ""
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ── Шаг 1: старт — сразу вход, реквизиты не спрашиваем ───────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Начать настройку: квартира выбирается из профиля после входа."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        self._client = MosRuClient()
        return await self.async_step_qr()

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
            self._notify_qr_auth()

        if not self._qr_task.done():
            return self.async_show_progress(
                step_id="qr",
                progress_action="scanning",
                progress_task=self._qr_task,
                description_placeholders={
                    "qr_url": self._qr_url,
                    "qr_link": self._qr_link,
                },
            )

        # Задача завершена
        try:
            result = self._qr_task.result()
        except Exception:
            self._qr_task = None
            return self.async_abort(reason="cannot_connect")

        self._qr_task = None

        if result == "login_incomplete":
            return self.async_abort(reason="login_incomplete")

        if result == "totp_required":
            pn_create(
                self.hass,
                message=(
                    "Вход подтверждён, mos.ru просит код из приложения-аутентификатора. "
                    "Введите его в Home Assistant.\n\n"
                    f"[Продолжить авторизацию]({_AUTH_SETTINGS_URL})"
                ),
                title="MOS.RU Water: Подтверждение входа",
                notification_id="mosru_water_qr",
            )
            return self.async_show_progress_done(next_step_id="totp")

        if result == "code_required":
            pn_create(
                self.hass,
                message=(
                    "Подтвердите вход в приложении mos.ru, затем введите "
                    "код из пуш-уведомления в Home Assistant.\n\n"
                    f"[Продолжить авторизацию]({_AUTH_SETTINGS_URL})"
                ),
                title="MOS.RU Water: Подтверждение входа",
                notification_id="mosru_water_qr",
            )
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
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates={CONF_SESSION_COOKIES: cookies},
            )

        return self.async_show_progress_done(next_step_id="place")

    def _notify_qr_auth(self) -> None:
        """Показать актуальную ссылку подтверждения той же сессии, что в QR."""
        prefix = "Требуется повторный вход в mos.ru. " if self._reauth_entry is not None else ""
        pn_create(
            self.hass,
            message=(
                f"{prefix}Подтвердите вход на mos.ru:\n\n"
                f"[Подтвердить вход]({self._qr_link})\n\n"
                "Или отсканируйте QR-код приложением **mos.ru** "
                f"или **Госуслуги Москвы**:\n\n![QR-код]({self._qr_url})"
            ),
            title="MOS.RU Water: Авторизация",
            notification_id="mosru_water_qr",
        )

    async def _poll_qr_scan(self) -> bool:
        """Фоновая задача: опросить QR до сканирования или истечения."""
        for tick in range(_QR_POLL_SECONDS):
            await asyncio.sleep(1)
            try:
                command = await self.hass.async_add_executor_job(self._client.poll_qr)
            except MosRuApiError as err:
                _LOGGER.error("QR poll error at tick %d: %s", tick, err)
                return False

            if command == "needComplete":
                try:
                    status = await self.hass.async_add_executor_job(
                        self._client.complete_qr_auth
                    )
                except MosRuAuthError as err:
                    # Новый QR тут не поможет: mos.ru ждёт шаг, который мы не умеем.
                    _LOGGER.error("QR-вход не завершён: %s", err)
                    return "login_incomplete"
                except MosRuApiError:
                    return False
                if status == "sms_required":
                    return "code_required"
                if status == "totp_required":
                    return "totp_required"
                return True

            if command == "askForConfirm":
                # Сервер отправил пуш «Подтвердить вход?» на телефон.
                # Продолжаем поллинг — после тапа «Подтвердить» придёт needComplete.
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
                    self._notify_qr_auth()
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
        return await self._async_submit_code(
            "code", self._client.submit_sms_and_trust, user_input
        )

    async def async_step_totp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ввод кода из приложения-аутентификатора (2FA)."""
        return await self._async_submit_code(
            "totp", self._client.submit_totp, user_input
        )

    async def _async_submit_code(
        self, step_id: str, submit, user_input: dict[str, Any] | None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("sms_code", "").strip()
            try:
                await self.hass.async_add_executor_job(submit, code)
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
                pn_dismiss(self.hass, notification_id="mosru_water_qr")

                if self._reauth_entry is not None:
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data_updates={CONF_SESSION_COOKIES: cookies},
                    )

                return await self.async_step_place()

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({
                vol.Required("sms_code"): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT
                    )
                ),
            }),
            errors=errors,
        )

    # ── Шаг 4: выбор квартиры ────────────────────────────────────────────

    def _load_places(self) -> list[dict]:
        """Войти в ed.mos.ru и получить квартиры профиля (синхронно, в executor)."""
        self._client.authorize_ed()
        return self._client.list_places()

    async def async_step_place(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Выбор квартиры из профиля «Электронного дома»."""
        if user_input and self._places:
            chosen = {p["user_place_id"]: p for p in self._places}.get(
                user_input.get(CONF_USER_PLACE_ID)
            )
            if chosen is not None:
                return await self._async_select_place(chosen)

        if self._places is None:
            try:
                self._places = await self.hass.async_add_executor_job(self._load_places)
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError as err:
                # Форма без полей: «Отправить» повторяет запрос.
                _LOGGER.error("Не удалось получить список квартир: %s", err)
                return self.async_show_form(
                    step_id="place",
                    data_schema=vol.Schema({}),
                    errors={"base": "cannot_get_places"},
                    description_placeholders={"error": str(err)},
                )

        if not self._places:
            return self.async_abort(reason="no_places")
        if len(self._places) == 1:
            return await self._async_select_place(self._places[0])

        return self.async_show_form(
            step_id="place",
            data_schema=vol.Schema({
                vol.Required(CONF_USER_PLACE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[
                        selector.SelectOptionDict(
                            value=p["user_place_id"], label=place_label(p)
                        )
                        for p in self._places
                    ])
                ),
            }),
        )

    async def _async_select_place(self, place: dict) -> FlowResult:
        """Запомнить квартиру: по userPlaceId идут все запросы к ed.mos.ru,
        paycode и flat нужны имени устройства и повторному поиску после выхода."""
        self._data[CONF_USER_PLACE_ID] = place["user_place_id"]
        self._data[CONF_PAYCODE] = place["paycode"]
        self._data[CONF_FLAT] = place["flat"]
        return await self.async_step_discover()


    # ── Шаг 5: выбор счётчиков ───────────────────────────────────────────

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Выбор счётчиков: автоматически по типу ХВС/ГВС, из списка или вручную."""
        errors: dict[str, str] = {}

        # Однократно запрашиваем счётчики выбранной квартиры
        if not self._counters_fetched:
            self._counters_fetched = True
            try:
                self._counters = await self.hass.async_add_executor_job(
                    self._client.get_counters, self._data[CONF_USER_PLACE_ID]
                )
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError:
                self._counters = []  # падаем в ручной ввод
            self._suggested_counters = pick_counters(self._counters)
            cold_id, hot_id = self._suggested_counters
            if cold_id and hot_id:
                # Обычная квартира: один ХВС и один ГВС — спрашивать нечего.
                self._data[CONF_COLD_ID] = cold_id
                self._data[CONF_HOT_ID] = hot_id
                return await self.async_step_sensors()

        # ── Счётчики найдены, но выбор неоднозначен ──────────────────────
        if self._counters:
            if user_input is not None:
                self._data[CONF_COLD_ID] = user_input[CONF_COLD_ID]
                self._data[CONF_HOT_ID]  = user_input[CONF_HOT_ID]
                return await self.async_step_sensors()

            def options(type_name: str) -> list:
                return [
                    selector.SelectOptionDict(
                        value=c["id"],
                        label=f"{c['name']} ({c['type']}, ID: {c['id']})",
                    )
                    for c in counters_of_type(self._counters, type_name)
                ]

            cold_id, hot_id = self._suggested_counters
            return self.async_show_form(
                step_id="discover",
                data_schema=vol.Schema({
                    vol.Required(
                        CONF_COLD_ID, description={"suggested_value": cold_id}
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options(COLD_TYPE))
                    ),
                    vol.Required(
                        CONF_HOT_ID, description={"suggested_value": hot_id}
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options(HOT_TYPE))
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

    # ── Шаг 6: HA-сенсоры ────────────────────────────────────────────────

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
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "logout"]
        )

    async def async_step_logout(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Forget the local session and start HA's existing reauth flow."""
        if user_input is None:
            return self.async_show_form(step_id="logout", data_schema=vol.Schema({}))

        # Stop polling and release the in-memory client before clearing cookies.
        if not await self.hass.config_entries.async_unload(self._entry.entry_id):
            return self.async_abort(reason="logout_failed")

        data = dict(self._entry.data)
        data[CONF_SESSION_COOKIES] = {}
        # This identifier belongs to the old ed.mos.ru profile; rediscover it.
        data.pop(CONF_USER_PLACE_ID, None)
        self.hass.config_entries.async_update_entry(self._entry, data=data)
        self._entry.async_start_reauth(self.hass)
        return self.async_abort(reason="reauth_started")

    async def async_step_settings(
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
            step_id="settings",
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
