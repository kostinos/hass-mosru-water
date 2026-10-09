"""DataUpdateCoordinator для mosru_water."""
from __future__ import annotations

import functools
import logging
import time
import threading
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
    normalized_reading,
)
from .const import (
    DOMAIN,
    CONF_PAYCODE, CONF_FLAT, CONF_USER_PLACE_ID,
    CONF_COLD_ID, CONF_HOT_ID,
    CONF_COLD_ENTITY, CONF_HOT_ENTITY, CONF_SUBMIT_DAY,
    CONF_SESSION_COOKIES,
    UPDATE_INTERVAL_MINUTES,
    ED_SESSION_REFRESH_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class MosRuWaterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Координатор: периодически проверяет нужно ли отправить показания."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._submitted_month: str | None = None
        self._client: MosRuClient | None = None
        # Authorization ed.mos.ru expires after one hour independently of acst.
        self._ed_authorized_at: float | None = None
        self._pending_user_place_id: str | None = None
        self._io_lock = threading.Lock()
        self._last_write_at = float('-inf')
        self._manual_pending = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
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
        self._ed_authorized_at = None

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

        if (self._ed_authorized_at is None
                or time.monotonic() - self._ed_authorized_at >= ED_SESSION_REFRESH_SECONDS):
            try:
                client.authorize_ed()
            except MosRuAuthError as err:
                self._invalidate_client()
                raise ConfigEntryAuthFailed(str(err)) from err
            self._ed_authorized_at = time.monotonic()
            _LOGGER.debug("Сессия ed.mos.ru обновлена через mos.ru")

        cfg = self._get_effective_config()
        user_place_id = cfg.get(CONF_USER_PLACE_ID)
        if not user_place_id:
            # Запись создана до перехода на ed.mos.ru — определяем и запоминаем,
            # чтобы не искать заново при каждом обновлении.
            user_place_id = client.find_user_place_id(
                cfg[CONF_PAYCODE], cfg.get(CONF_FLAT, "")
            )
            self._pending_user_place_id = user_place_id
            _LOGGER.debug('Определён профиль Электронного дома')

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

    def _persist_operation_guard(self, result: dict[str, Any]) -> None:
        """Persist an uncertain mutation so restarting HA cannot retry it automatically."""
        data = dict(self._entry.data)
        if result.get('last_status') == 'partial':
            data['submission_blocked_month'] = self._current_month()
            data['last_operation'] = result.get('operation_results', {})
        else:
            data.pop('submission_blocked_month', None)
            data.pop('last_operation', None)
        if data != self._entry.data:
            self.hass.config_entries.async_update_entry(self._entry, data=data)

    def _read_sensor(self, entity_id: str) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            raise UpdateFailed(f"Сенсор {entity_id} недоступен")
        try:
            value = float(state.state)
            normalized_reading(value)
            reported = getattr(state, 'last_reported', None) or state.last_updated
            if (dt_util.now() - reported).total_seconds() > 48 * 3600:
                raise UpdateFailed(f'Сенсор {entity_id} не обновлялся более 48 часов')
            if state.attributes.get('unit_of_measurement') != 'm³':
                raise UpdateFailed(f'Сенсор {entity_id} должен возвращать м³')
            return value
        except (ValueError, MosRuApiError) as err:
            raise UpdateFailed(
                f"Недопустимое значение сенсора {entity_id}"
            ) from err

    def _fetch_device_info(self) -> dict[str, Any]:
        """Получить текущий статус счётчиков из API (синхронно)."""
        if not self._io_lock.acquire(blocking=False):
            raise MosRuTemporaryError('Операция с mos.ru уже выполняется')
        try:
            return self._fetch_device_info_unlocked()
        finally:
            self._io_lock.release()

    def _fetch_device_info_unlocked(self) -> dict[str, Any]:
        cfg = self._get_effective_config()
        client, user_place_id = self._prepare_client()

        try:
            try:
                device_map = client.get_device_info(user_place_id)
            except MosRuAuthError:
                # Reading is safe to repeat; never retry PUT/DELETE this way.
                _LOGGER.info("Сессия ed.mos.ru отклонена; восстанавливаем перед повторным чтением")
                self._ed_authorized_at = None
                client, user_place_id = self._prepare_client()
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
        return await self._async_manual_submit(replace=False)

    async def _async_manual_submit(self, *, replace: bool) -> dict[str, Any]:
        last = max(self._last_write_at, getattr(self, '_last_manual_at', float('-inf')))
        if self._manual_pending or self._io_lock.locked() or time.monotonic() - last < 60:
            raise UpdateFailed('Операция уже выполняется или недавно выполнялась; попробуйте позже')
        self._manual_pending = True
        self._last_manual_at = time.monotonic()
        try:
            result = await self.hass.async_add_executor_job(
                functools.partial(self._submit, replace=replace))
            self._persist_cookies()
            self._persist_user_place_id()
            self._persist_operation_guard(result)
            return result
        except ConfigEntryAuthFailed as err:
            if getattr(err, "operation_result", None):
                self._persist_operation_guard(err.operation_result)
            raise
        finally:
            self._manual_pending = False

    async def async_replace_readings(self) -> dict[str, Any]:
        """Перезаписать показания за текущий период.

        Удаляет последнее показание каждого счётчика и отправляет новое. Вызывается
        только вручную (сервис mosru_water.replace_readings): удаляется последняя
        запись независимо от того, кто её внёс — она могла прийти от управляющей
        компании, а не от интеграции.
        """
        return await self._async_manual_submit(replace=True)

    def _submit(self, *, replace: bool = False) -> dict[str, Any]:
        """Serialize reads/writes on the Session and reject overlapping mutations."""
        if not self._io_lock.acquire(blocking=False):
            raise UpdateFailed('Операция с mos.ru уже выполняется')
        try:
            if time.monotonic() - self._last_write_at < 60:
                raise UpdateFailed('Подождите минуту перед повторной отправкой')
            self._last_write_at = time.monotonic()
            return self._submit_unlocked(replace=replace)
        finally:
            self._io_lock.release()

    def _submit_unlocked(self, *, replace: bool = False) -> dict[str, Any]:
        """Отправить показания на ed.mos.ru.

        replace=True — сначала удалить последнее показание, чтобы перезаписать
        значение за уже закрытый период. Вызывается только по явной команде
        пользователя (сервис mosru_water.replace_readings).
        """
        cfg = self._get_effective_config()
        if (cfg[CONF_COLD_ENTITY] == cfg[CONF_HOT_ENTITY]
                or cfg[CONF_COLD_ID] == cfg[CONF_HOT_ID]):
            raise UpdateFailed('Холодная и горячая вода должны использовать разные сенсоры и счётчики')
        cold_val = normalized_reading(self._read_sensor(cfg[CONF_COLD_ENTITY]))
        hot_val  = normalized_reading(self._read_sensor(cfg[CONF_HOT_ENTITY]))
        client, user_place_id = self._prepare_client()

        # Validate BOTH targets before any PUT/DELETE, including manually supplied IDs.
        try:
            device_map = client.get_device_info(user_place_id)
        except MosRuAuthError as err:
            self._invalidate_client()
            raise ConfigEntryAuthFailed('Сессия портала истекла') from err
        closed_current = set()
        for counter_id, value in ((cfg[CONF_COLD_ID], cold_val), (cfg[CONF_HOT_ID], hot_val)):
            info = device_map.get(counter_id)
            if not info:
                raise UpdateFailed('Счётчик не найден в выбранной квартире')
            if info.get('readonly', True):
                if not replace and str(info.get('reading_period', ''))[:7] == self._current_month():
                    closed_current.add(counter_id)
                    continue
                raise UpdateFailed('Портал не разрешает изменение показаний этого счётчика')
            previous = info.get('current_reading')
            try:
                previous = float(previous)
                normalized_reading(previous)
            except (ValueError, TypeError, MosRuApiError) as err:
                raise UpdateFailed('Не удалось проверить предыдущее показание портала') from err
            if value < previous or value - previous > 100:
                raise UpdateFailed('Показание уменьшилось или выросло более чем на 100 м³; проверьте его на портале')
            if replace and str(info.get('reading_period', ''))[:7] != self._current_month():
                raise UpdateFailed('Можно заменять только показания текущего месяца')

        already: list[str] = []
        outcomes: dict[str, Any] = {}

        def submit_one(counter_id: str, value: float, label: str) -> dict[str, Any] | None:
            if counter_id in closed_current:
                already.append(label)
                outcomes[label] = {'stage': 'already_submitted'}
                return None
            if replace:
                latest = client.get_device_info(user_place_id).get(counter_id, {})
                expected = device_map[counter_id]
                if (latest.get('readonly', True)
                        or latest.get('reading_period') != expected.get('reading_period')
                        or latest.get('current_reading') != expected.get('current_reading')):
                    raise MosRuApiError('Показание на портале изменилось; удаление отменено')
                outcomes[label] = {'stage': 'delete_unknown', 'value': value}
                try:
                    client.remove_last_indication(user_place_id, counter_id)
                except (MosRuAuthError, MosRuApiError) as err:
                    if not isinstance(err, MosRuTemporaryError):
                        outcomes[label]["stage"] = "delete_rejected"
                    raise
                outcomes[label]['stage'] = 'send_unknown_after_delete'
            else:
                outcomes[label] = {'stage': 'send_unknown', 'value': value}
            try:
                response = client.send_reading(user_place_id, counter_id, value)
                outcomes[label]['stage'] = 'submitted'
                return response
            except MosRuAlreadySubmittedError:
                # Портал не перезаписывает показание за период: это не сбой,
                # а сигнал «уже сдано». Перезапись — отдельной командой.
                _LOGGER.info("%s: показание за период уже внесено на портале", label)
                already.append(label)
                outcomes[label]['stage'] = 'already_submitted'
                return None
            except (MosRuAuthError, MosRuApiError) as err:
                if not isinstance(err, MosRuTemporaryError):
                    outcomes[label]["stage"] = "send_rejected_after_delete" if replace else "send_rejected"
                raise

        try:
            cold_resp = submit_one(cfg[CONF_COLD_ID], cold_val, "холодная")
            hot_resp  = submit_one(cfg[CONF_HOT_ID], hot_val, "горячая")
        except (MosRuAuthError, MosRuApiError) as err:
            changed_or_unknown = any(item['stage'] in {
                'submitted', 'delete_unknown', 'send_unknown',
                'send_unknown_after_delete', 'send_rejected_after_delete'
            } for item in outcomes.values())
            if not changed_or_unknown:
                if isinstance(err, MosRuAuthError):
                    self._invalidate_client()
                    raise ConfigEntryAuthFailed('Сессия портала истекла') from err
                if isinstance(err, MosRuTemporaryError):
                    raise
                raise UpdateFailed('Портал отклонил запись; показания не изменены') from err
            # A timed-out PUT/DELETE may have succeeded remotely. Never delete again
            # automatically, and make uncertainty visible rather than reporting success.
            _LOGGER.error('Запись показаний завершилась частично или с неопределённым результатом')
            if isinstance(err, MosRuAuthError):
                self._invalidate_client()
            self._submitted_month = self._current_month()
            partial = {'last_status': 'partial', 'operation_results': outcomes}
            for label, key in (('холодная', 'last_cold'), ('горячая', 'last_hot')):
                if outcomes.get(label, {}).get('stage') == 'submitted':
                    partial[key] = outcomes[label]['value']
                    partial['last_submitted_at'] = dt_util.now()
            if isinstance(err, MosRuAuthError):
                auth_failure = ConfigEntryAuthFailed("Сессия портала истекла после частичной записи")
                auth_failure.operation_result = partial
                raise auth_failure from err
            return partial

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

        result = {
            "last_status":       "already_submitted" if len(already) == 2 else "success",
            "last_submitted_at": submitted_at,
            "cold_response":     cold_resp,
            "hot_response":      hot_resp,
            "operation_results": outcomes,
        }
        if 'холодная' not in already:
            result['last_cold'] = cold_val
        if 'горячая' not in already:
            result['last_hot'] = hot_val
        if len(already) == 2:
            result.pop('last_submitted_at')
        return result

    async def _async_update_data(self) -> dict[str, Any]:
        """Вызывается каждые 45 минут. Всегда опрашивает статус; отправляет в нужный день."""
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
        for key in ("last_cold", "last_hot", "last_status", "last_submitted_at", "operation_results"):
            if key in prev:
                result[key] = prev[key]
        result.update(device_data)

        cfg = self._get_effective_config()
        submit_day = int(cfg.get(CONF_SUBMIT_DAY, 20))
        blocked = cfg.get('submission_blocked_month') == self._current_month()
        if blocked:
            result['last_status'] = 'partial'
            result['operation_results'] = cfg.get('last_operation', {})
        if (
            datetime.now().day == submit_day
            and self._submitted_month != self._current_month()
            and not blocked
        ):
            try:
                submit_result = await self.hass.async_add_executor_job(self._submit)
                self._persist_cookies()
                self._persist_operation_guard(submit_result)
                result.update(submit_result)
            except MosRuTemporaryError as err:
                # _submitted_month не выставлен — попробуем снова через час,
                # пока день отправки не закончился.
                _LOGGER.warning(
                    "mos.ru временно недоступен, отправка показаний отложена: %s", err
                )
            except ConfigEntryAuthFailed as err:
                if getattr(err, "operation_result", None):
                    self._persist_operation_guard(err.operation_result)
                raise
            except UpdateFailed:
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
