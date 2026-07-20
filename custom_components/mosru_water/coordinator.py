"""DataUpdateCoordinator для mosru_water."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
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

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._submitted_month: str | None = None
        self._client: MosRuClient | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )

    def _current_month(self) -> str:
        return datetime.now().strftime("%Y-%m")

    def _get_effective_config(self) -> dict[str, Any]:
        data = dict(self._entry.data)
        if self._entry.options:
            data.update(self._entry.options)
        return data

    def _get_client(self) -> MosRuClient:
        """Вернуть кешированный клиент или создать из сохранённых cookies."""
        if self._client is None:
            cookies = self._get_effective_config().get(CONF_SESSION_COOKIES, {})
            if not cookies:
                raise ConfigEntryAuthFailed(
                    "Нет сохранённой сессии, требуется повторная авторизация"
                )
            client = MosRuClient()
            client.restore_session(cookies)
            self._client = client
            _LOGGER.debug("Создан новый MosRuClient из сохранённых cookies")
        return self._client

    def _invalidate_client(self) -> None:
        """Сбросить кешированный клиент (вызывать при ошибке авторизации)."""
        self._client = None

    def _persist_cookies(self) -> None:
        """Сохранить текущие cookies клиента обратно в config entry.

        Вызывать из event loop после успешного API-вызова.
        mos.ru обновляет TTL cookie при каждом запросе — без этого
        сохранённые cookies стареют даже при активном использовании.
        """
        if self._client is None:
            return
        new_cookies = self._client.get_session_cookies()
        current = self._get_effective_config().get(CONF_SESSION_COOKIES, {})
        if new_cookies == current:
            return
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_SESSION_COOKIES: new_cookies},
        )
        _LOGGER.debug("Cookies сессии обновлены в config entry (%d ключей)", len(new_cookies))

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

    def _fetch_device_info(self) -> dict[str, Any]:
        """Получить текущий статус счётчиков из API (синхронно)."""
        cfg = self._get_effective_config()
        try:
            client = self._get_client()
        except ConfigEntryAuthFailed:
            raise

        try:
            device_map = client.get_device_info(cfg[CONF_PAYCODE], cfg[CONF_FLAT])
        except MosRuAuthError as err:
            self._invalidate_client()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MosRuApiError as err:
            raise UpdateFailed(f"Ошибка получения статуса: {err}") from err

        cold_info = device_map.get(cfg.get(CONF_COLD_ID, ""), {})
        hot_info  = device_map.get(cfg.get(CONF_HOT_ID, ""), {})

        return {
            "cold_current":           cold_info.get("current_reading"),
            "hot_current":            hot_info.get("current_reading"),
            "cold_readonly":          cold_info.get("readonly", True),
            "hot_readonly":           hot_info.get("readonly", True),
            "cold_inspection_date":   cold_info.get("inspection_date"),
            "hot_inspection_date":    hot_info.get("inspection_date"),
            "cold_inspection_status": cold_info.get("inspection_status", ""),
            "hot_inspection_status":  hot_info.get("inspection_status", ""),
            "cold_reading_period":    cold_info.get("reading_period"),
            "hot_reading_period":     hot_info.get("reading_period"),
            "cold_number":            cold_info.get("number"),
            "hot_number":             hot_info.get("number"),
        }

    async def async_submit_now(self) -> dict[str, Any]:
        """Отправить показания прямо сейчас (вызывается из button.py)."""
        result = await self.hass.async_add_executor_job(self._submit)
        self._persist_cookies()
        return result

    def _submit(self) -> dict[str, Any]:
        cfg = self._get_effective_config()
        cold_val = self._read_sensor(cfg[CONF_COLD_ENTITY])
        hot_val  = self._read_sensor(cfg[CONF_HOT_ENTITY])

        try:
            client = self._get_client()
        except ConfigEntryAuthFailed:
            raise

        try:
            cold_resp = client.send_reading(
                cfg[CONF_PAYCODE], cfg[CONF_FLAT], cfg[CONF_COLD_ID], cold_val
            )
            hot_resp = client.send_reading(
                cfg[CONF_PAYCODE], cfg[CONF_FLAT], cfg[CONF_HOT_ID], hot_val
            )
        except MosRuAuthError as err:
            self._invalidate_client()
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
        """Вызывается каждый час. Всегда опрашивает статус; отправляет в нужный день."""
        try:
            device_data = await self.hass.async_add_executor_job(self._fetch_device_info)
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise
        except Exception as err:
            raise UpdateFailed(f"Неожиданная ошибка: {err}") from err

        # Сохраняем обновлённые cookies (mos.ru обновляет TTL при каждом запросе)
        self._persist_cookies()

        prev = self.data or {}
        result: dict[str, Any] = {}
        for key in ("last_cold", "last_hot", "last_status", "last_submitted_at"):
            if key in prev:
                result[key] = prev[key]
        result.update(device_data)

        cfg = self._get_effective_config()
        submit_day = int(cfg.get(CONF_SUBMIT_DAY, 20))
        if (
            datetime.now().day == submit_day
            and self._submitted_month != self._current_month()
        ):
            try:
                submit_result = await self.hass.async_add_executor_job(self._submit)
                self._persist_cookies()
                result.update(submit_result)
            except (UpdateFailed, ConfigEntryAuthFailed):
                raise
            except Exception as err:
                raise UpdateFailed(f"Неожиданная ошибка при отправке: {err}") from err

        return result

    def update_config(self, new_data: dict[str, Any]) -> None:
        """Обновить конфиг (вызывается при изменении options).

        Самого _entry обновлять не нужно — HA уже сделал это до вызова.
        Сбрасываем кешированный клиент, чтобы он пересоздался из актуальных cookies.
        """
        self._invalidate_client()
