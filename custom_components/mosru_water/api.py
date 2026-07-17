"""Клиент mos.ru для передачи показаний счётчиков воды."""
from __future__ import annotations

import logging
from datetime import datetime

import requests

_LOGGER = logging.getLogger(__name__)

_LOGIN_OAUTH_URL = "https://login.mos.ru/sps/oauth/ae"
_QR_PULL_URL     = "https://login.mos.ru/sps/login/methods/headless/qrCode/pull"
_QR_REFRESH_URL  = "https://login.mos.ru/sps/login/methods/headless/qrCode/refresh"
_QR_COMPLETE_URL = "https://login.mos.ru/sps/login/methods/qrCode/complete"
_AJAX_URL        = "https://www.mos.ru/pgu/common/ajax/index.php"
_TIMEOUT         = 30
_USER_AGENT      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class MosRuAuthError(Exception):
    """Ошибка авторизации."""


class MosRuApiError(Exception):
    """Ошибка API."""


class MosRuClient:
    """HTTP-клиент для работы с mos.ru."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    # ── QR OAuth ──────────────────────────────────────────────────────────

    def start_qr_session(self) -> dict:
        """Инициализировать OAuth-сессию и получить данные QR-кода.

        Returns: {"link": str, "expires": float}
        """
        try:
            self._session.get(
                _LOGIN_OAUTH_URL,
                params={
                    "client_id":     "mos.ru",
                    "response_type": "code",
                    "scope":         "profile openid contacts usr_grps esia",
                    "redirect_uri":  "https://www.mos.ru/api/acs/v1/login/satisfy",
                },
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
            resp = self._session.get(
                _QR_PULL_URL,
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            data = resp.json()
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err

        if data.get("command") != "showQRCode":
            raise MosRuApiError(
                f"QR-сессия не запустилась: команда={data.get('command')!r}"
            )

        return {"link": data["link"], "expires": data.get("expires", 0)}

    def poll_qr(self) -> str:
        """Опросить статус QR-сессии.

        Returns: command — showQRCode | askForConfirm | needComplete | needRefresh
        """
        try:
            resp = self._session.get(
                _QR_PULL_URL,
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            return resp.json().get("command", "")
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

    def refresh_qr(self) -> dict:
        """Обновить истёкший QR-код.

        Returns: {"link": str, "expires": float}
        """
        try:
            resp = self._session.post(
                _QR_REFRESH_URL,
                json={},
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            data = resp.json()
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err
        return {"link": data.get("link", ""), "expires": data.get("expires", 0)}

    def complete_qr_auth(self) -> None:
        """Завершить QR-авторизацию и установить www.mos.ru сессию."""
        try:
            resp = self._session.post(
                _QR_COMPLETE_URL,
                data={},
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err
        if resp.status_code >= 400:
            raise MosRuAuthError("Ошибка завершения QR-авторизации")

    def get_session_cookies(self) -> dict:
        """Вернуть текущие cookies для сохранения в конфиге."""
        return {c.name: c.value for c in self._session.cookies}

    def restore_session(self, cookies: dict) -> None:
        """Восстановить сессию из сохранённых cookies."""
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        for name, value in cookies.items():
            self._session.cookies.set(name, value)

    # ── API ───────────────────────────────────────────────────────────────

    def get_counters(self, paycode: str, flat: str) -> list[dict]:
        """Получить список счётчиков из личного кабинета."""
        try:
            resp = self._session.post(
                _AJAX_URL,
                data={
                    "ajaxModule":     "Guis",
                    "ajaxAction":     "getCountersInfo",
                    "items[paycode]": paycode,
                    "items[flat]":    flat,
                },
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

        if resp.status_code in (401, 403):
            raise MosRuAuthError("Сессия истекла, требуется повторная авторизация")

        try:
            data = resp.json()
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err

        if not isinstance(data, dict) or "data" not in data:
            raise MosRuApiError(f"Неожиданный ответ: {data}")

        counters = data["data"].get("counters", [])
        return [
            {
                "id":   str(c.get("counterId") or c.get("num", "")),
                "name": c.get("counterName", ""),
                "type": c.get("type", ""),
            }
            for c in counters
        ]

    def send_reading(
        self,
        paycode: str,
        flat: str,
        counter_id: str,
        value_m3: float,
    ) -> dict:
        """Передать показание счётчика (в м³)."""
        period = datetime.now().strftime("%Y-%m")
        try:
            resp = self._session.post(
                _AJAX_URL,
                data={
                    "ajaxModule":          "Guis",
                    "ajaxAction":          "addCounterInfo",
                    "items[paycode]":       paycode,
                    "items[flat]":          flat,
                    "items[counterNum]":    counter_id,
                    "items[counterVal][0]": f"{value_m3:.3f}",
                    "items[period]":        period,
                },
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

        if resp.status_code in (401, 403):
            raise MosRuAuthError("Сессия истекла, требуется повторная авторизация")

        try:
            return resp.json()
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err
