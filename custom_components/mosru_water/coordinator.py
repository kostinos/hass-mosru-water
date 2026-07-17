"""DataUpdateCoordinator для mosru_water."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MosRuAuthError, MosRuApiError, MosRuClient
from .const import (
    DOMAIN,
    CONF_PAYCODE, CONF_FLAT,
    CONF_COLD_ID, CONF_HOT_ID,
    CONF_COLD_ENTITY, CONF_HOT_ENTITY, CONF_SUBMIT_DAY,
    CONF_SESSION_COOKIES,
    UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)


class MosRuWaterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Координатор: периодически проверяет нужно ли отправить показания."""

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        self._entry_data = entry_data
        self._submitted_month: str | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )

    def _current_month(self) -> str:
        return datetime.now().strftime("%Y-%m")

    def _read_sensor(self, entity_id: str) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            raise UpdateFailed(f"Сенсор {entity_id} недоступен")
        try:
            return float(state.state)
        except ValueError as err:
            raise UpdateFailed(
                f"Не удалось прочитать значение {entity_id}: {state.state}"
            ) from err

    def _get_effective_config(self) -> dict[str, Any]:
        return dict(self._entry_data)

    def _make_client(self, cfg: dict[str, Any]) -> MosRuClient:
        """Создать клиент с восстановленной сессией."""
        cookies = cfg.get(CONF_SESSION_COOKIES, {})
        if not cookies:
            raise UpdateFailed("Нет сохранённой сессии, требуется повторная авторизация")
        client = MosRuClient()
        client.restore_session(cookies)
        return client

    async def async_submit_now(self) -> dict[str, Any]:
        """Отправить показания прямо сейчас (вызывается из button.py)."""
        return await self.hass.async_add_executor_job(self._submit)

    def _submit(self) -> dict[str, Any]:
        cfg = self._get_effective_config()
        cold_val = self._read_sensor(cfg[CONF_COLD_ENTITY])
        hot_val  = self._read_sensor(cfg[CONF_HOT_ENTITY])

        try:
            client = self._make_client(cfg)
        except UpdateFailed as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        try:
            cold_resp = client.send_reading(
                cfg[CONF_PAYCODE], cfg[CONF_FLAT], cfg[CONF_COLD_ID], cold_val
            )
            hot_resp = client.send_reading(
                cfg[CONF_PAYCODE], cfg[CONF_FLAT], cfg[CONF_HOT_ID], hot_val
            )
        except MosRuAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MosRuApiError as err:
            raise UpdateFailed(f"Ошибка отправки: {err}") from err

        self._submitted_month = self._current_month()
        submitted_at = datetime.now().isoformat(timespec="seconds")

        _LOGGER.info(
            "Показания отправлены: холодная=%.3f м³, горячая=%.3f м³",
            cold_val, hot_val,
        )

        return {
            "last_cold":         cold_val,
            "last_hot":          hot_val,
            "last_status":       "success",
            "last_submitted_at": submitted_at,
            "cold_response":     cold_resp,
            "hot_response":      hot_resp,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Вызывается каждый час. Отправляет только если настал нужный день и ещё не отправляли."""
        cfg = self._get_effective_config()
        submit_day = int(cfg.get(CONF_SUBMIT_DAY, 20))
        today = datetime.now()

        if today.day != submit_day:
            return self.data or {}

        if self._submitted_month == self._current_month():
            return self.data or {}

        try:
            result = await self.hass.async_add_executor_job(self._submit)
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise
        except Exception as err:
            raise UpdateFailed(f"Неожиданная ошибка: {err}") from err

        return result

    def update_config(self, new_data: dict[str, Any]) -> None:
        """Обновить конфиг (вызывается при изменении options)."""
        self._entry_data = new_data
