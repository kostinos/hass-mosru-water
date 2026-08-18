"""DataUpdateCoordinator для mosru_water."""
from __future__ import annotations

import functools
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MosRuAlreadySubmittedError,
    MosRuAuthError,
    MosRuApiError,
    MosRuClient,
    MosRuTemporaryError,
)
from .const import (
    DOMAIN,
    CONF_PAYCODE, CONF_FLAT, CONF_USER_PLACE_ID,
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
        # Сессия ed.mos.ru живёт вместе с клиентом: повторный OAuth на каждый
        # запрос не нужен, сбрасывается вместе с клиентом.
        self._ed_authorized = False
        self._pending_user_place_id: str | None = None

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
        return self._client

    def _invalidate_client(self) -> None:
        """Сбросить кешированный клиент (вызывать при ошибке авторизации)."""
        self._client = None
        self._ed_authorized = False

    def _prepare_client(self) -> tuple[MosRuClient, str]:
        """Подготовить клиент к работе с ed.mos.ru и вернуть его с userPlaceId.

        Выполняется синхронно в executor. Порядок важен: probe обновляет acst
        (иначе OAuth для ed.mos.ru не пройдёт), затем вход в ed.mos.ru, и лишь
        потом можно спрашивать userPlaceId.
        """
        client = self._get_client()

        # probe обновляет acst через silent OAuth если тот истёк (как браузер).
        if not client.try_refresh_acst():
            self._invalidate_client()
            raise ConfigEntryAuthFailed(
                "Сессия mos.ru истекла, требуется повторная авторизация"
            )

        if not self._ed_authorized:
            try:
                client.authorize_ed()
            except MosRuAuthError as err:
                self._invalidate_client()
                raise ConfigEntryAuthFailed(str(err)) from err
            self._ed_authorized = True

        cfg = self._get_effective_config()
        user_place_id = cfg.get(CONF_USER_PLACE_ID)
        if not user_place_id:
            # Запись создана до перехода на ed.mos.ru — определяем и запоминаем,
            # чтобы не искать заново при каждом обновлении.
            user_place_id = client.find_user_place_id(
                cfg[CONF_PAYCODE], cfg.get(CONF_FLAT, "")
            )
            self._pending_user_place_id = user_place_id
            _LOGGER.info("Определён userPlaceId для ed.mos.ru: %s", user_place_id)

        return client, str(user_place_id)

    def _persist_user_place_id(self) -> None:
        """Сохранить найденный userPlaceId в config entry (из event loop)."""
        upid = self._pending_user_place_id
        if not upid:
            return
        self._pending_user_place_id = None
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_USER_PLACE_ID: upid},
        )

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
        client, user_place_id = self._prepare_client()

        try:
            device_map = client.get_device_info(user_place_id)
        except MosRuAuthError as err:
            self._invalidate_client()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MosRuTemporaryError:
            raise  # обрабатывается в _async_update_data — не роняем данные
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
        self._persist_user_place_id()
        return result

    async def async_replace_readings(self) -> dict[str, Any]:
        """Перезаписать показания за текущий период.

        Удаляет последнее показание каждого счётчика и отправляет новое. Вызывается
        только вручную (сервис mosru_water.replace_readings): удаляется последняя
        запись независимо от того, кто её внёс — она могла прийти от управляющей
        компании, а не от интеграции.
        """
        result = await self.hass.async_add_executor_job(
            functools.partial(self._submit, replace=True)
        )
        self._persist_cookies()
        self._persist_user_place_id()
        return result

    def _submit(self, *, replace: bool = False) -> dict[str, Any]:
        """Отправить показания на ed.mos.ru.

        replace=True — сначала удалить последнее показание, чтобы перезаписать
        значение за уже закрытый период. Вызывается только по явной команде
        пользователя (сервис mosru_water.replace_readings).
        """
        cfg = self._get_effective_config()
        cold_val = self._read_sensor(cfg[CONF_COLD_ENTITY])
        hot_val  = self._read_sensor(cfg[CONF_HOT_ENTITY])
        client, user_place_id = self._prepare_client()

        already: list[str] = []

        def submit_one(counter_id: str, value: float, label: str) -> dict[str, Any] | None:
            if replace:
                client.remove_last_indication(user_place_id, counter_id)
            try:
                return client.send_reading(user_place_id, counter_id, value)
            except MosRuAlreadySubmittedError:
                # Портал не перезаписывает показание за период: это не сбой,
                # а сигнал «уже сдано». Перезапись — отдельной командой.
                _LOGGER.info("%s: показание за период уже внесено на портале", label)
                already.append(label)
                return None

        try:
            cold_resp = submit_one(cfg[CONF_COLD_ID], cold_val, "холодная")
            hot_resp  = submit_one(cfg[CONF_HOT_ID], hot_val, "горячая")
        except MosRuAuthError as err:
            self._invalidate_client()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MosRuTemporaryError:
            raise  # отправку повторит следующий цикл координатора
        except MosRuApiError as err:
            raise UpdateFailed(f"Ошибка отправки: {err}") from err

        self._submitted_month = self._current_month()
        # Aware datetime: сенсор объявлен device_class TIMESTAMP, HA требует tzinfo.
        submitted_at = dt_util.now()

        if len(already) == 2:
            _LOGGER.info(
                "Показания за текущий период уже внесены на портале, отправка не требуется"
            )
        else:
            _LOGGER.info(
                "Показания отправлены: холодная=%.3f м³, горячая=%.3f м³",
                cold_val, hot_val,
            )

        return {
            "last_cold":         cold_val,
            "last_hot":          hot_val,
            "last_status":       "already_submitted" if len(already) == 2 else "success",
            "last_submitted_at": submitted_at,
            "cold_response":     cold_resp,
            "hot_response":      hot_resp,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Вызывается каждый час. Всегда опрашивает статус; отправляет в нужный день."""
        try:
            device_data = await self.hass.async_add_executor_job(self._fetch_device_info)
        except MosRuTemporaryError as err:
            # mos.ru периодически отвечает retry_later. Ретраи внутри клиента уже
            # исчерпаны — держим прошлые показания, чтобы сенсоры не уходили
            # в unavailable до следующего цикла.
            if self.data:
                _LOGGER.warning(
                    "mos.ru временно недоступен (%s), оставляем предыдущие данные", err
                )
                return self.data
            raise UpdateFailed(f"mos.ru временно недоступен: {err}") from err
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise
        except Exception as err:
            raise UpdateFailed(f"Неожиданная ошибка: {err}") from err

        # Сохраняем обновлённые cookies (mos.ru обновляет TTL при каждом запросе)
        self._persist_cookies()
        self._persist_user_place_id()

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
            except MosRuTemporaryError as err:
                # _submitted_month не выставлен — попробуем снова через час,
                # пока день отправки не закончился.
                _LOGGER.warning(
                    "mos.ru временно недоступен, отправка показаний отложена: %s", err
                )
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
