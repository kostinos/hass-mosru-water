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
        factory = qrcode.image.svg.SvgPathImage
        qr_img = qrcode.make(link, image_factory=factory, border=2)
        qr_img.save(qr_path)
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
        self._client: MosRuClient | None = None
        self._qr_task: asyncio.Task | None = None
        self._qr_url: str = ""
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
            self._qr_url = await self.hass.async_add_executor_job(
                _write_qr_svg, www_dir, qr_data["link"], ts
            )
            self._qr_task = self.hass.async_create_task(self._poll_qr_scan())

        if not self._qr_task.done():
            return self.async_show_progress(
                step_id="qr",
                progress_action="scanning",
                progress_task=self._qr_task,
                description_placeholders={"qr_url": self._qr_url},
            )

        # Задача завершена
        try:
            success: bool = self._qr_task.result()
        except Exception:
            self._qr_task = None
            return self.async_abort(reason="cannot_connect")

        self._qr_task = None

        if not success:
            # QR истёк или ошибка — начать заново
            return await self.async_step_qr()

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
        for _ in range(_QR_POLL_SECONDS):
            await asyncio.sleep(1)
            try:
                command = await self.hass.async_add_executor_job(self._client.poll_qr)
            except MosRuApiError:
                return False

            if command == "needComplete":
                try:
                    await self.hass.async_add_executor_job(
                        self._client.complete_qr_auth
                    )
                    return True
                except (MosRuAuthError, MosRuApiError):
                    return False

            if command == "needRefresh":
                try:
                    qr_data = await self.hass.async_add_executor_job(
                        self._client.refresh_qr
                    )
                    ts = int(time.time())
                    www_dir = self.hass.config.path("www")
                    self._qr_url = await self.hass.async_add_executor_job(
                        _write_qr_svg, www_dir, qr_data["link"], ts
                    )
                except MosRuApiError:
                    return False
                continue

            # showQRCode / askForConfirm — продолжаем опрос

        return False  # таймаут

    # ── Шаг 3: выбор счётчиков ───────────────────────────────────────────

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Выбор счётчиков из списка mos.ru."""
        if not self._counters:
            try:
                self._counters = await self.hass.async_add_executor_job(
                    self._client.get_counters,
                    self._data[CONF_PAYCODE],
                    self._data[CONF_FLAT],
                )
            except MosRuAuthError:
                return self.async_abort(reason="session_expired")
            except MosRuApiError:
                return self.async_abort(reason="cannot_get_counters")

            if not self._counters:
                return self.async_abort(reason="no_counters")

        counter_options = [
            selector.SelectOptionDict(value=c["id"], label=f"{c['name']} (ID: {c['id']})")
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
        )

    # ── Шаг 4: HA-сенсоры ────────────────────────────────────────────────

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

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
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
