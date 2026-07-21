"""Клиент mos.ru для передачи показаний счётчиков воды."""
from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime

import requests

_LOGGER = logging.getLogger(__name__)

_LOGIN_PAGE_URL  = "https://login.mos.ru/sps/login/methods/password"
_LOGIN_BO        = (
    "/sps/oauth/ae"
    "?scope=profile+openid+contacts+usr_grps+esia"
    "&response_type=code"
    "&redirect_uri=https://www.mos.ru/api/acs/v1/login/satisfy"
    "&client_id=mos.ru"
)
_QR_PULL_URL       = "https://login.mos.ru/sps/login/methods/headless/qrCode/pull"
_QR_REFRESH_URL    = "https://login.mos.ru/sps/login/methods/headless/qrCode/refresh"
_QR_COMPLETE_URL   = "https://login.mos.ru/sps/login/methods/qrCode/complete"
_QR_ASKTOTRUST_URL = "https://login.mos.ru/sps/login/ur/askToTrust"
_SMS_URL           = "https://login.mos.ru/sps/login/methods/sms"
_UTILITY_METER_URL = "https://www.mos.ru/api/utility-meter/v1"
_SERVICE_PAGE_URL  = "https://www.mos.ru/services/pokazaniya-vodi-i-tepla/new/"
_TIMEOUT         = 30
_USER_AGENT      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _parse_form(html: str) -> tuple[str, dict]:
    """Извлечь action формы и скрытые поля из HTML."""
    action = ""
    action_m = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if action_m:
        action = action_m.group(1)
    hidden: dict[str, str] = {}
    for inp in re.finditer(r'<input([^>]+)>', html, re.IGNORECASE):
        attrs = inp.group(1)
        type_m = re.search(r'type=["\'](\w+)["\']', attrs, re.IGNORECASE)
        if not type_m or type_m.group(1).lower() != "hidden":
            continue
        name_m = re.search(r'name=["\']([^"\']+)["\']', attrs)
        value_m = re.search(r'value=["\']([^"\']*)["\']', attrs)
        if name_m:
            hidden[name_m.group(1)] = value_m.group(1) if value_m else ""
    return action, hidden


class MosRuAuthError(Exception):
    """Ошибка авторизации."""


class MosRuApiError(Exception):
    """Ошибка API."""


class MosRuClient:
    """HTTP-клиент для работы с mos.ru."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._login_referer = "https://login.mos.ru/"
        self._poll_counter: int = int(datetime.now().timestamp() * 1000)

    # ── QR OAuth ──────────────────────────────────────────────────────────

    def start_qr_session(self) -> dict:
        """Инициализировать OAuth-сессию и получить данные QR-кода.

        Returns: {"link": str, "expires": float}
        """
        try:
            # www.mos.ru/api/acs/v1/login устанавливает ACS-SESSID и yabm,
            # без которых satisfy не может завершить обмен кода (возвращает 500).
            # Следуем редиректу на login.mos.ru/sps/oauth/ae — он регистрирует
            # authorization request и устанавливает oauth_az.
            resp_init = self._session.get(
                "https://www.mos.ru/api/acs/v1/login",
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
            self._login_referer = resp_init.url
            _LOGGER.debug(
                "start_qr_session: oauth init final_url=%s cookies=%s",
                resp_init.url,
                {c.name: c.domain for c in self._session.cookies},
            )
            # POST refresh создаёт QR-сессию
            resp = self._session.post(
                _QR_REFRESH_URL,
                data="{}",
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "text/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._login_referer,
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                raise MosRuApiError(f"QR-сессия: редирект → {resp.headers.get('Location', '?')[:100]}")
            data = resp.json()
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err

        if not data.get("link"):
            raise MosRuApiError(f"QR-сессия не запустилась: {data!r}")

        return {"link": data["link"], "expires": data.get("expires", 0)}

    def poll_qr(self) -> str:
        """Опросить статус QR-сессии.

        Returns: command — showQRCode | askForConfirm | needComplete | needRefresh
        """
        try:
            self._poll_counter += 1
            resp = self._session.get(
                _QR_PULL_URL,
                params={"_": self._poll_counter},
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._login_referer,
                },
                allow_redirects=False,
                timeout=_TIMEOUT,
            )
            _LOGGER.debug(
                "poll_qr: status=%d content-type=%s",
                resp.status_code,
                resp.headers.get("Content-Type", "?"),
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", "?")
                _LOGGER.error("poll_qr: редирект → %s", loc[:200])
                raise MosRuApiError(f"Редирект: {loc[:100]}")
            try:
                return resp.json().get("command", "")
            except ValueError:
                _LOGGER.error(
                    "poll_qr: не JSON (status=%d): %r",
                    resp.status_code,
                    resp.text[:600],
                )
                raise MosRuApiError("Неожиданный формат ответа")
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

    def refresh_qr(self) -> dict:
        """Обновить истёкший QR-код.

        Returns: {"link": str, "expires": float}
        """
        try:
            resp = self._session.post(
                _QR_REFRESH_URL,
                data="{}",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "text/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self._login_referer,
                },
                timeout=_TIMEOUT,
            )
            data = resp.json()
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err
        return {"link": data.get("link", ""), "expires": data.get("expires", 0)}

    def complete_qr_auth(self) -> str:
        """Завершить QR-авторизацию: POST qrCode/complete.

        Returns: 'done' | 'sms_required'
        """
        try:
            resp = self._session.post(
                _QR_COMPLETE_URL,
                data={},
                headers={"Referer": self._login_referer},
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err
        _LOGGER.debug(
            "complete_qr_auth: final_url=%s status=%d cookies=%s",
            resp.url,
            resp.status_code,
            {c.name: c.domain for c in self._session.cookies},
        )
        if resp.status_code >= 400:
            raise MosRuAuthError("Ошибка завершения QR-авторизации")
        if "methods2/sms" in resp.url or "/methods/sms" in resp.url:
            # Парсим форму страницы — нам нужны action и скрытые поля
            self._sms_page_url = resp.url
            self._sms_form_action, self._sms_hidden = _parse_form(resp.text)
            _LOGGER.debug(
                "complete_qr_auth: SMS form action=%r hidden_keys=%s",
                self._sms_form_action,
                list(self._sms_hidden.keys()),
            )
            return "sms_required"
        return "done"

    def submit_sms_and_trust(self, code: str) -> None:
        """Отправить 6-значный код SMS/пуша и довериться устройству."""
        page_url = getattr(self, "_sms_page_url", "")
        form_action = getattr(self, "_sms_form_action", "")
        hidden = dict(getattr(self, "_sms_hidden", {}))

        # Формируем URL для POST из action формы
        if form_action.startswith("http"):
            sms_url = form_action
        elif form_action.startswith("/"):
            sms_url = f"https://login.mos.ru{form_action}"
        else:
            bo_param = urllib.parse.quote(_LOGIN_BO, safe="")
            sms_url = f"{_SMS_URL}?bo={bo_param}"

        post_data = {**hidden, "sms-code": code}
        _LOGGER.debug(
            "submit_sms_and_trust: POST %s fields=%s",
            sms_url,
            list(post_data.keys()),
        )

        try:
            resp = self._session.post(
                sms_url,
                data=post_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": page_url or sms_url,
                },
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

        _LOGGER.debug(
            "submit_sms_and_trust: after SMS POST final_url=%s status=%d body_start=%r",
            resp.url,
            resp.status_code,
            resp.text[:300],
        )
        if resp.status_code >= 400:
            raise MosRuAuthError("Неверный SMS-код")

        # Если попали на askToTrust — доверяемся устройству
        if "askToTrust" in resp.url:
            trust_page_url = resp.url
            trust_form_action, trust_hidden = _parse_form(resp.text)
            _LOGGER.debug(
                "submit_sms_and_trust: askToTrust form action=%r hidden_keys=%s",
                trust_form_action,
                list(trust_hidden.keys()),
            )
            if trust_form_action.startswith("http"):
                trust_url = trust_form_action
            elif trust_form_action.startswith("/"):
                trust_url = f"https://login.mos.ru{trust_form_action}"
            else:
                trust_url = trust_page_url or _QR_ASKTOTRUST_URL

            trust_data = {**trust_hidden, "action": "trust"}
            try:
                resp2 = self._session.post(
                    trust_url,
                    data=trust_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": trust_page_url,
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-User": "?1",
                    },
                    allow_redirects=False,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as err:
                raise MosRuApiError(f"Сетевая ошибка: {err}") from err

            # Следуем редиректам вручную, логируя каждый шаг
            for step in range(15):
                loc = resp2.headers.get("Location", "")
                if resp2.status_code not in (301, 302, 303, 307, 308) or not loc:
                    break
                if not loc.startswith("http"):
                    loc = urllib.parse.urljoin(resp2.url, loc)
                _LOGGER.debug("trust redirect %d: %s -> %s", step, resp2.url, loc)
                # satisfy требует sec-fetch-* заголовки как у браузерной навигации
                req_headers: dict[str, str] = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": resp2.url,
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-site",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                }
                try:
                    resp2 = self._session.get(
                        loc,
                        headers=req_headers,
                        allow_redirects=False,
                        timeout=_TIMEOUT,
                    )
                except requests.RequestException as err:
                    raise MosRuApiError(f"Сетевая ошибка: {err}") from err
                _LOGGER.debug(
                    "trust step %d: status=%d url=%s set-cookie=%r body=%r",
                    step,
                    resp2.status_code,
                    resp2.url,
                    resp2.headers.get("Set-Cookie", "")[:200],
                    resp2.text[:200],
                )

            _LOGGER.debug(
                "submit_sms_and_trust: final url=%s status=%d cookies=%s",
                resp2.url,
                resp2.status_code,
                {c.name: c.domain for c in self._session.cookies},
            )
            if resp2.status_code >= 400 and resp2.status_code < 500:
                raise MosRuAuthError("Ошибка доверия устройству")
            cookie_names = {c.name for c in self._session.cookies}
            if "Ltpatoken2" not in cookie_names:
                raise MosRuAuthError("Авторизация не завершена: Ltpatoken2 не установлен")

    def try_refresh_acst(self) -> bool:
        """Обновить acst через ACS login с существующим Ltpatoken2.

        acst выдаётся с Max-Age=3600; браузер обновляет его тихо через этот же
        endpoint. Возвращает True если сессия осталась живой (финальный URL на www.mos.ru).
        """
        try:
            resp = self._session.get(
                "https://www.mos.ru/api/acs/v1/login",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.mos.ru/",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
            alive = resp.url.startswith("https://www.mos.ru") and resp.status_code == 200
            _LOGGER.debug(
                "try_refresh_acst: final_url=%s status=%d set-cookie=%r alive=%s",
                resp.url, resp.status_code,
                resp.headers.get("Set-Cookie", "")[:200],
                alive,
            )
            return alive
        except requests.RequestException as err:
            _LOGGER.warning("try_refresh_acst failed: %s", err)
            return False

    def warm_session(self) -> None:
        """GET главной и /pgu/ mos.ru — инициализирует сессии портала после OAuth."""
        warm_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        for url in (
            "https://www.mos.ru/",
            _SERVICE_PAGE_URL,
        ):
            try:
                resp = self._session.get(
                    url,
                    headers=warm_headers,
                    allow_redirects=True,
                    timeout=_TIMEOUT,
                )
                _LOGGER.debug(
                    "warm_session %s: status=%d final_url=%s set-cookie=%r",
                    url,
                    resp.status_code,
                    resp.url,
                    resp.headers.get("Set-Cookie", "")[:200],
                )
            except requests.RequestException as err:
                _LOGGER.warning("warm_session %s failed: %s", url, err)

    def get_session_cookies(self) -> dict:
        """Вернуть текущие cookies с доменами для сохранения в конфиге."""
        return {
            c.name: {"value": c.value, "domain": c.domain or "", "path": c.path or "/"}
            for c in self._session.cookies
        }

    def restore_session(self, cookies: dict) -> None:
        """Восстановить сессию из сохранённых cookies (с доменами)."""
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._login_referer = "https://login.mos.ru/"
        for name, data in cookies.items():
            if isinstance(data, dict):
                self._session.cookies.set(
                    name, data["value"],
                    domain=data.get("domain") or None,
                    path=data.get("path") or "/",
                )
            else:
                # обратная совместимость: старый формат name → value (строка)
                self._session.cookies.set(name, data)

    # ── API ───────────────────────────────────────────────────────────────

    def get_counters(self, paycode: str, flat: str) -> list[dict]:
        """Получить список счётчиков из личного кабинета."""
        key_cookies = {
            c.name: (c.value[:40] if c.value else "", c.domain, c.path)
            for c in self._session.cookies
            if c.name in ("Ltpatoken2", "acst", "ACS-SESSID", "yabm")
        }
        _LOGGER.debug("get_counters: key_cookies=%s", key_cookies)

        url = f"{_UTILITY_METER_URL}/device"
        params = {"flat": flat, "payer_code": paycode}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": _SERVICE_PAGE_URL,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        prepared = self._session.prepare_request(
            requests.Request("GET", url, params=params, headers=headers)
        )
        cookie_hdr = prepared.headers.get("Cookie", "(none)")
        _LOGGER.debug(
            "get_counters: url=%s Cookie len=%d val=%r",
            prepared.url,
            len(cookie_hdr),
            cookie_hdr[:800],
        )

        try:
            resp = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

        _LOGGER.debug(
            "get_counters: status=%d body_start=%r",
            resp.status_code,
            resp.text[:600],
        )
        if resp.status_code in (401, 403):
            raise MosRuAuthError("Сессия истекла, требуется повторная авторизация")

        try:
            data = resp.json()
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err

        if data.get("code") != "SUCCESS" or "data" not in data:
            raise MosRuApiError(f"Неожиданный ответ: {repr(data)[:200]}")

        payload = data["data"]
        # Devices with warnings (e.g. upcoming inspection) land in top-level
        # "errors" as {"device": {...}} instead of data.active — collect both.
        devices = list(payload.get("active", []))
        for err_item in data.get("errors", []):
            dev = err_item.get("device") if isinstance(err_item, dict) else None
            if dev:
                devices.append(dev)
        devices += payload.get("inactive", [])

        result = []
        seen: set[str] = set()
        for c in devices:
            model = c.get("model", {})
            if model.get("class") != "water":
                continue
            dev_id = str(c.get("id", ""))
            if dev_id in seen:
                continue
            seen.add(dev_id)
            result.append({
                "id":   dev_id,
                "name": c.get("number", ""),
                "type": model.get("type", ""),
            })
        return result

    def get_device_info(self, paycode: str, flat: str) -> dict[str, dict]:
        """Получить текущий статус счётчиков: показания, поверка, доступность отправки.

        Returns: {device_id: {type, number, current_reading, reading_period,
                               readonly, inspection_date, inspection_status}}
        """
        url = f"{_UTILITY_METER_URL}/device"
        params = {"flat": flat, "payer_code": paycode}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": _SERVICE_PAGE_URL,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        try:
            resp = self._session.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

        _LOGGER.debug("get_device_info: status=%d body=%r", resp.status_code, resp.text[:600])

        if resp.status_code in (401, 403):
            raise MosRuAuthError("Сессия истекла, требуется повторная авторизация")

        try:
            data = resp.json()
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err

        if data.get("code") != "SUCCESS" or "data" not in data:
            raise MosRuApiError(f"Неожиданный ответ: {repr(data)[:200]}")

        payload = data["data"]
        devices = list(payload.get("active", []))
        for err_item in data.get("errors", []):
            dev = err_item.get("device") if isinstance(err_item, dict) else None
            if dev:
                devices.append(dev)

        result: dict[str, dict] = {}
        for c in devices:
            model = c.get("model", {})
            if model.get("class") != "water":
                continue
            dev_id = str(c.get("id", ""))
            if not dev_id or dev_id in result:
                continue
            rr = c.get("recent_reading") or {}
            result[dev_id] = {
                "type":               model.get("type", ""),
                "number":             c.get("number", ""),
                "current_reading":    rr.get("indication"),
                "reading_period":     rr.get("period"),
                "readonly":           rr.get("readonly", False),
                "inspection_date":    c.get("inspection_date"),
                "inspection_status":  c.get("inspection_status", ""),
            }
        return result

    def send_reading(
        self,
        paycode: str,
        flat: str,
        counter_id: str,
        value_m3: float,
    ) -> dict:
        """Передать показание счётчика (в м³)."""
        period = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "device_id": counter_id,
            "indication": round(value_m3, 3),
            "period": period,
        }
        _LOGGER.debug("send_reading: payload=%s", payload)
        try:
            resp = self._session.post(
                f"{_UTILITY_METER_URL}/reading",
                json=payload,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": _SERVICE_PAGE_URL,
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuApiError(f"Сетевая ошибка: {err}") from err

        _LOGGER.debug(
            "send_reading: status=%d body=%r",
            resp.status_code,
            resp.text[:400],
        )
        if resp.status_code in (401, 403):
            raise MosRuAuthError("Сессия истекла, требуется повторная авторизация")

        try:
            return resp.json()
        except ValueError as err:
            raise MosRuApiError("Неожиданный формат ответа") from err
