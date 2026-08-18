"""Клиент mos.ru для передачи показаний счётчиков воды."""
from __future__ import annotations

import calendar
import logging
import re
import time
import urllib.parse
from datetime import date, datetime

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
_SERVICE_PAGE_URL  = "https://www.mos.ru/services/pokazaniya-vodi-i-tepla/new/"

# ed.mos.ru (Электронный дом) — рабочий API показаний. Прежний
# www.mos.ru/api/utility-meter/v1 не существует: POST /reading всегда отдавал 404,
# из-за чего показания годами «отправлялись» вникуда.
_ED_URL          = "https://ed.mos.ru"
_ED_API          = f"{_ED_URL}/api"
_ED_COUNTERS     = f"{_ED_API}/efp/counters"
_ED_PAGE_URL     = f"{_ED_URL}/lk/counters/"
# OAuth ed.mos.ru: при живой SSO-сессии проходит молча, без ввода пароля.
_ED_REDIRECT_URI = f"{_ED_URL}/security/callback/sudir/login"
_ED_OAUTH_URL    = (
    "https://login.mos.ru/sps/oauth/ae"
    "?scope=openid+profile"
    "&access_type=offline"
    "&response_type=code"
    f"&redirect_uri={_ED_REDIRECT_URI}"
    "&client_id=ed.mos.ru"
)
_TIMEOUT         = 30
_USER_AGENT      = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
# my.mos.ru и ed.mos.ru отдают 403 без полного набора браузерных заголовков —
# антибот смотрит именно на них, а не на cookies.
_BROWSER_HINTS   = {
    "Accept-Language": "ru,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
_XHR_HEADERS     = {
    **_BROWSER_HINTS,
    "Accept": "application/json, text/plain, */*",
    "Referer": _ED_PAGE_URL,
    "Origin": _ED_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
_NAV_HEADERS     = {
    **_BROWSER_HINTS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Временные сбои mos.ru: сервис отвечает 200 с {"error": true, "code": "retry_later"}
# либо обычным 5xx. Такие ответы не означают проблем с сессией — нужен повтор.
_TRANSIENT_STATUS  = (429, 500, 502, 503, 504)
_TRANSIENT_CODES   = {"retry_later", "service_unavailable", "temporarily_unavailable"}
_RETRY_ATTEMPTS    = 2    # дополнительные попытки для идемпотентных запросов
_RETRY_DELAY       = 5    # пауза между попытками, сек


def _period_end_of_month(today: date | None = None) -> str:
    """Последний день текущего месяца в формате YYYY-MM-DD.

    ed.mos.ru привязывает показание к расчётному периоду, а не к дате отправки:
    в запросе всегда стоит конец месяца (например 2026-08-31).
    """
    day = today or date.today()
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=last).isoformat()


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


class MosRuTemporaryError(MosRuApiError):
    """Сервис mos.ru временно недоступен — имеет смысл повторить позже."""


class MosRuAlreadySubmittedError(MosRuApiError):
    """Показание за этот период уже внесено на портале.

    ed.mos.ru отвечает 400 «Показание за данный период уже внесено» и не
    перезаписывает значение. Чтобы заменить его, нужно сначала удалить прежнее
    через remove_last_indication() — это делается только по явной команде
    пользователя, автоматика показания не удаляет.
    """


def _parse_api_response(resp: requests.Response) -> dict:
    """Проверить HTTP-ответ API и вернуть распарсенный JSON.

    Raises:
        MosRuAuthError: сессия истекла (401/403).
        MosRuTemporaryError: временный сбой mos.ru (5xx, 429, code=retry_later).
        MosRuApiError: прочие ошибки.
    """
    if resp.status_code in (401, 403):
        raise MosRuAuthError("Сессия истекла, требуется повторная авторизация")
    if resp.status_code in _TRANSIENT_STATUS:
        raise MosRuTemporaryError(
            f"HTTP {resp.status_code}, сервис временно недоступен"
        )

    try:
        data = resp.json()
    except ValueError as err:
        raise MosRuApiError("Неожиданный формат ответа") from err
    if not isinstance(data, dict):
        raise MosRuApiError(f"Неожиданный формат ответа: {repr(data)[:200]}")

    code = str(data.get("code", "")).lower()
    if code in _TRANSIENT_CODES:
        raise MosRuTemporaryError(
            data.get("message") or f"сервис временно недоступен ({code})"
        )
    # ed.mos.ru сообщает об ошибке в поле "error" строкой, а не флагом.
    err_text = data.get("error") if isinstance(data.get("error"), str) else None
    if err_text and "уже внесено" in err_text:
        raise MosRuAlreadySubmittedError(err_text)
    # Прочие HTTP-ошибки (404 на неверный эндпоинт, 400 на плохой payload и т.п.).
    # Проверяем после разбора JSON, чтобы включить в сообщение текст от сервера.
    if not resp.ok:
        raise MosRuApiError(
            f"HTTP {resp.status_code}: "
            f"{err_text or data.get('message') or repr(data)[:200]}"
        )
    if err_text:
        raise MosRuApiError(f"Ошибка API: {err_text}")
    if data.get("error") is True:
        raise MosRuApiError(f"Ошибка API: {repr(data)[:200]}")
    return data


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
        if resp.status_code >= 400:
            raise MosRuAuthError("Ошибка завершения QR-авторизации")
        if "methods2/sms" in resp.url or "/methods/sms" in resp.url:
            self._sms_page_url = resp.url
            self._sms_form_action, self._sms_hidden = _parse_form(resp.text)
            return "sms_required"
        return "done"

    def submit_sms_and_trust(self, code: str) -> None:
        """Отправить 6-значный код SMS/пуша и довериться устройству."""
        page_url = getattr(self, "_sms_page_url", "")
        form_action = getattr(self, "_sms_form_action", "")
        hidden = dict(getattr(self, "_sms_hidden", {}))

        if form_action.startswith("http"):
            sms_url = form_action
        elif form_action.startswith("/"):
            sms_url = f"https://login.mos.ru{form_action}"
        else:
            bo_param = urllib.parse.quote(_LOGIN_BO, safe="")
            sms_url = f"{_SMS_URL}?bo={bo_param}"

        post_data = {**hidden, "sms-code": code}
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

        if resp.status_code >= 400:
            raise MosRuAuthError("Неверный SMS-код")

        # Если попали на askToTrust — доверяемся устройству
        if "askToTrust" in resp.url:
            trust_page_url = resp.url
            trust_form_action, trust_hidden = _parse_form(resp.text)
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

            # Следуем редиректам вручную
            for step in range(15):
                loc = resp2.headers.get("Location", "")
                if resp2.status_code not in (301, 302, 303, 307, 308) or not loc:
                    break
                if not loc.startswith("http"):
                    loc = urllib.parse.urljoin(resp2.url, loc)
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

            if resp2.status_code >= 400 and resp2.status_code < 500:
                raise MosRuAuthError("Ошибка доверия устройству")
            cookie_names = {c.name for c in self._session.cookies}
            if "Ltpatoken2" not in cookie_names:
                raise MosRuAuthError("Авторизация не завершена: Ltpatoken2 не установлен")

    def try_refresh_acst(self) -> bool:
        """Обновить acst через официальный ACS probe endpoint.

        Браузер вызывает /api/acs/v1/probe при каждой загрузке страницы.
        Если acst истёк, probe тихо делает OAuth через Ltpatoken2 и выдаёт
        новый acst перед редиректом на back_url?status=200.
        Возвращает True если сессия жива (финальный URL содержит status=200).
        """
        back_url = "https://www.mos.ru/shared/acs/core0521.html?fieldName=status"
        try:
            resp = self._session.get(
                "https://www.mos.ru/api/acs/v1/probe",
                params={"back_url": back_url},
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
            return "status=200" in resp.url
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
                self._session.get(
                    url,
                    headers=warm_headers,
                    allow_redirects=True,
                    timeout=_TIMEOUT,
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

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        retries: int = 0,
        **kwargs,
    ) -> dict:
        """Выполнить запрос к API и вернуть проверенный JSON.

        retries — число дополнительных попыток при временном сбое mos.ru
        (использовать только для идемпотентных запросов).
        """
        last_err = MosRuTemporaryError("Запрос не был выполнен")
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(_RETRY_DELAY)
            try:
                resp = self._session.request(method, url, timeout=_TIMEOUT, **kwargs)
            except requests.RequestException as err:
                last_err = MosRuTemporaryError(f"Сетевая ошибка: {err}")
                continue
            try:
                return _parse_api_response(resp)
            except MosRuTemporaryError as err:
                last_err = err
        raise last_err

    # ── ed.mos.ru ─────────────────────────────────────────────────────────

    def authorize_ed(self) -> None:
        """Авторизоваться в ed.mos.ru поверх действующей SSO-сессии mos.ru.

        При живом Ltpatoken2 OAuth проходит молча: login.mos.ru редиректит на
        callback с ?code=, который обменивается на сессионную cookie ed.mos.ru.
        Дальше все запросы к API идут по cookies, без токенов в query.
        """
        try:
            resp = self._session.get(
                _ED_OAUTH_URL,
                headers=_NAV_HEADERS,
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuTemporaryError(f"Сетевая ошибка при входе в ed.mos.ru: {err}") from err

        code_m = re.search(r"[?&]code=([^&]+)", resp.url)
        if not code_m:
            # Сессия SSO истекла — цепочка ушла на форму логина вместо callback.
            raise MosRuAuthError("Не удалось авторизоваться в ed.mos.ru: код не получен")

        try:
            auth = self._session.post(
                f"{_ED_API}/profile/auth/web",
                params={"code": code_m.group(1)},
                headers={**_XHR_HEADERS, "Referer": resp.url},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MosRuTemporaryError(f"Сетевая ошибка ed.mos.ru auth: {err}") from err

        if auth.status_code in (401, 403):
            raise MosRuAuthError("ed.mos.ru отклонил авторизацию")
        if not auth.ok:
            raise MosRuApiError(f"ed.mos.ru auth: HTTP {auth.status_code}")

    def _counters_payload(self, user_place_id: str) -> list[dict]:
        """Сырой ответ listByPayerCode: список квартир со счётчиками."""
        data = self._request_json(
            "GET",
            f"{_ED_COUNTERS}/listByPayerCode/",
            params={"userPlaceIds": user_place_id},
            headers=_XHR_HEADERS,
            retries=_RETRY_ATTEMPTS,
        )
        places = data.get("data")
        if not isinstance(places, list):
            raise MosRuApiError(f"Неожиданный ответ: {repr(data)[:200]}")
        return places

    def find_user_place_id(self, paycode: str, flat: str) -> str:
        """Определить userPlaceId по коду плательщика — для мастера настройки.

        ed.mos.ru адресует квартиру своим userPlaceId, а не paycode. Профиль
        пользователя перечисляет квартиры в data.addresses; в каждой записи есть
        fls (это и есть код плательщика), flat и userPlaceId.
        """
        data = self._request_json(
            "GET",
            f"{_ED_API}/profile/user/getInfo/",
            headers=_XHR_HEADERS,
            retries=_RETRY_ATTEMPTS,
        )
        addresses = (data.get("data") or {}).get("addresses") or []
        for place in addresses:
            if not isinstance(place, dict):
                continue
            # flat в ответе — число, paycode тоже: сравниваем как строки
            if str(place.get("fls") or "") != str(paycode):
                continue
            if flat and str(place.get("flat") or "") != str(flat):
                continue
            upid = place.get("userPlaceId")
            if upid:
                return str(upid)
        raise MosRuApiError(
            f"В профиле ed.mos.ru не найдена квартира с кодом плательщика {paycode}"
        )

    def get_counters(self, user_place_id: str) -> list[dict]:
        """Список счётчиков воды для мастера настройки."""
        result: list[dict] = []
        for place in self._counters_payload(user_place_id):
            for c in place.get("activeCounters") or []:
                counter_id = str(c.get("counterId") or "")
                if not counter_id:
                    continue
                result.append({
                    "id":   counter_id,
                    "name": c.get("num", ""),
                    "type": c.get("typeName", ""),   # ХВС / ГВС
                })
        return result

    def get_device_info(self, user_place_id: str) -> dict[str, dict]:
        """Текущий статус счётчиков: показания, поверка, доступность отправки.

        Returns: {counter_id: {type, number, current_reading, reading_period,
                               readonly, inspection_date, inspection_status}}
        """
        result: dict[str, dict] = {}
        for place in self._counters_payload(user_place_id):
            for c in place.get("activeCounters") or []:
                counter_id = str(c.get("counterId") or "")
                if not counter_id or counter_id in result:
                    continue
                last = c.get("lastIndication") or {}
                result[counter_id] = {
                    "type":              c.get("typeName", ""),
                    "number":            c.get("num", ""),
                    "current_reading":   last.get("indication"),
                    "reading_period":    last.get("period"),
                    # enableTransfer=False — портал сейчас не принимает показания
                    "readonly":          not c.get("enableTransfer", True),
                    "inspection_date":   c.get("checkUpDate"),
                    "inspection_status": c.get("checkupStatus", ""),
                }
        return result

    def send_reading(
        self,
        user_place_id: str,
        counter_id: str,
        value_m3: float,
        period: str | None = None,
    ) -> dict:
        """Передать показание счётчика (в м³).

        period — конец расчётного месяца (YYYY-MM-DD), как ждёт портал; по
        умолчанию последний день текущего месяца.
        Значение уходит целым числом: портал принимает только целые м³.

        Raises:
            MosRuAlreadySubmittedError: за этот период показание уже внесено.
        """
        # Без ретраев: повтор может создать дубль. Временный сбой поднимается
        # наружу, отправку повторит координатор.
        data = self._request_json(
            "PUT",
            f"{_ED_COUNTERS}/addIndications/",
            params={
                "userPlaceId": user_place_id,
                "counterId": counter_id,
                "indication": int(round(value_m3)),
                "period": period or _period_end_of_month(),
            },
            headers=_XHR_HEADERS,
        )
        if not (data.get("data") or {}).get("result"):
            raise MosRuApiError(f"Показание не принято: {repr(data)[:200]}")
        return data

    def remove_last_indication(self, user_place_id: str, counter_id: str) -> dict:
        """Удалить последнее показание счётчика на портале.

        Нужно, чтобы перезаписать показание за уже закрытый период: сам
        addIndications значение не заменяет, а отвечает 400.

        Вызывается ТОЛЬКО по явной команде пользователя: удаляется последнее
        показание независимо от того, кто его внёс (портал помечает источник в
        поле source — запись могла прийти от управляющей компании).
        """
        data = self._request_json(
            "DELETE",
            f"{_ED_COUNTERS}/removeLastValue/",
            params={"counterId": counter_id, "userPlaceId": user_place_id},
            headers=_XHR_HEADERS,
        )
        if not (data.get("data") or {}).get("result"):
            raise MosRuApiError(f"Показание не удалено: {repr(data)[:200]}")
        return data
