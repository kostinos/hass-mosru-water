"""Тесты клиента ed.mos.ru. Сеть не используется: сессия подменяется заглушкой.

Запуск:  python3 -m unittest discover -s tests -v
Требуется requests (импортируется api.py). Homeassistant не нужен — api.py от него
не зависит.

Ответы портала взяты из реальных HAR-записей: важно, чтобы тесты проверяли
фактические форматы ed.mos.ru, а не представление о них.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

# Грузим api.py напрямую, минуя пакет: mosru_water/__init__.py импортирует
# homeassistant и voluptuous, а сам api.py от них не зависит.
_API_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "mosru_water" / "api.py"
)
_spec = importlib.util.spec_from_file_location("mosru_water_api", _API_PATH)
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

MosRuAlreadySubmittedError = api.MosRuAlreadySubmittedError
MosRuApiError = api.MosRuApiError
MosRuAuthError = api.MosRuAuthError
MosRuClient = api.MosRuClient
MosRuTemporaryError = api.MosRuTemporaryError
_parse_api_response = api._parse_api_response
_period_end_of_month = api._period_end_of_month


class FakeResponse:
    """Минимальная замена requests.Response для _parse_api_response."""

    def __init__(self, status_code: int, payload, *, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        if self._payload is _INVALID:
            raise ValueError("not json")
        return self._payload


_INVALID = object()


class PeriodTest(unittest.TestCase):
    """period в запросе — конец расчётного месяца, а не дата отправки."""

    def test_end_of_month(self):
        self.assertEqual(_period_end_of_month(date(2026, 8, 19)), "2026-08-31")

    def test_february_leap(self):
        self.assertEqual(_period_end_of_month(date(2024, 2, 5)), "2024-02-29")

    def test_february_non_leap(self):
        self.assertEqual(_period_end_of_month(date(2026, 2, 5)), "2026-02-28")

    def test_last_day_stays(self):
        self.assertEqual(_period_end_of_month(date(2026, 4, 30)), "2026-04-30")

    def test_december(self):
        self.assertEqual(_period_end_of_month(date(2026, 12, 1)), "2026-12-31")


class ParseResponseTest(unittest.TestCase):
    """Разбор ответов: главное — не считать провал успехом."""

    def test_success_passthrough(self):
        payload = {"data": {"result": True, "counterId": 1158038}}
        self.assertEqual(_parse_api_response(FakeResponse(200, payload)), payload)

    def test_already_submitted_is_dedicated_error(self):
        # Реальный ответ ed.mos.ru при повторной отправке за тот же период.
        resp = FakeResponse(400, {"code": 400, "error": "Показание за данный период уже внесено."})
        with self.assertRaises(MosRuAlreadySubmittedError):
            _parse_api_response(resp)

    def test_404_is_error_not_success(self):
        """Регресс: POST /reading отдавал 404, а код считал это успехом."""
        payload = {"code": "NOT_FOUND", "data": None,
                   "message": 'No route found for "POST /api/utility-meter/v1/reading"',
                   "errors": []}
        with self.assertRaises(MosRuApiError) as ctx:
            _parse_api_response(FakeResponse(404, payload))
        self.assertNotIsInstance(ctx.exception, MosRuTemporaryError)
        self.assertIn("404", str(ctx.exception))

    def test_auth_errors(self):
        for code in (401, 403):
            with self.subTest(code=code), self.assertRaises(MosRuAuthError):
                _parse_api_response(FakeResponse(code, {}))

    def test_transient_statuses_are_retryable(self):
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code), self.assertRaises(MosRuTemporaryError):
                _parse_api_response(FakeResponse(code, {}))

    def test_retry_later_code_is_transient(self):
        resp = FakeResponse(200, {"code": "retry_later", "message": "позже"})
        with self.assertRaises(MosRuTemporaryError):
            _parse_api_response(resp)

    def test_other_error_string_is_api_error(self):
        resp = FakeResponse(400, {"code": 400, "error": "Некорректное показание"})
        with self.assertRaises(MosRuApiError) as ctx:
            _parse_api_response(resp)
        self.assertNotIsInstance(ctx.exception, MosRuAlreadySubmittedError)

    def test_non_json_body(self):
        with self.assertRaises(MosRuApiError):
            _parse_api_response(FakeResponse(200, _INVALID, text="<html>"))

    def test_non_dict_json(self):
        with self.assertRaises(MosRuApiError):
            _parse_api_response(FakeResponse(200, [1, 2, 3]))


class ClientCallsTest(unittest.TestCase):
    """Форма запросов к ed.mos.ru: метод, URL и параметры."""

    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session

    def _reply(self, status, payload):
        self.session.request.return_value = FakeResponse(status, payload)

    def test_send_reading_uses_put_and_int_indication(self):
        self._reply(200, {"data": {"result": True}})
        self.client.send_reading("1152619", "1158038", 483.74, period="2026-08-31")

        method, url = self.session.request.call_args[0]
        params = self.session.request.call_args.kwargs["params"]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/efp/counters/addIndications/"))
        self.assertEqual(params["userPlaceId"], "1152619")
        self.assertEqual(params["counterId"], "1158038")
        # Портал принимает только целые м³
        self.assertEqual(params["indication"], 484)
        self.assertIsInstance(params["indication"], int)
        self.assertEqual(params["period"], "2026-08-31")

    def test_send_reading_defaults_period_to_end_of_month(self):
        self._reply(200, {"data": {"result": True}})
        self.client.send_reading("1152619", "1158038", 100.0)
        params = self.session.request.call_args.kwargs["params"]
        self.assertEqual(params["period"], _period_end_of_month())

    def test_send_reading_rejects_result_false(self):
        self._reply(200, {"data": {"result": False}})
        with self.assertRaises(MosRuApiError):
            self.client.send_reading("1152619", "1158038", 100.0)

    def test_send_reading_propagates_already_submitted(self):
        self._reply(400, {"code": 400, "error": "Показание за данный период уже внесено."})
        with self.assertRaises(MosRuAlreadySubmittedError):
            self.client.send_reading("1152619", "1158038", 100.0)

    def test_send_reading_does_not_retry(self):
        """Повтор PUT мог бы создать дубль — ретраи здесь запрещены."""
        self._reply(503, {})
        with self.assertRaises(MosRuTemporaryError):
            self.client.send_reading("1152619", "1158038", 100.0)
        self.assertEqual(self.session.request.call_count, 1)

    def test_remove_last_indication_uses_delete(self):
        self._reply(200, {"data": {"result": True}})
        self.client.remove_last_indication("1152619", "1158038")

        method, url = self.session.request.call_args[0]
        params = self.session.request.call_args.kwargs["params"]
        self.assertEqual(method, "DELETE")
        self.assertTrue(url.endswith("/efp/counters/removeLastValue/"))
        self.assertEqual(params, {"counterId": "1158038", "userPlaceId": "1152619"})


# Ответ listByPayerCode, сокращённый до используемых полей (из HAR).
_COUNTERS_PAYLOAD = {
    "data": [{
        "userPlaceId": 1152619,
        "fls": "1730249056",
        "flat": "218",
        "activeCounters": [
            {
                "counterId": 1158038,
                "typeName": "ХВС",
                "num": "14-007378",
                "checkUpDate": "2032-07-16",
                "checkupStatus": "OK",
                "enableTransfer": True,
                "lastIndication": {"period": "2026-08-31", "indication": 483.0, "source": "22"},
            },
            {
                "counterId": 1158039,
                "typeName": "ГВС",
                "num": "14-087265",
                "checkUpDate": "2032-07-16",
                "checkupStatus": "OK",
                "enableTransfer": False,
                "lastIndication": {"period": "2026-08-31", "indication": 279.0, "source": "22"},
            },
        ],
    }]
}


class CountersParsingTest(unittest.TestCase):
    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session
        self.session.request.return_value = FakeResponse(200, _COUNTERS_PAYLOAD)

    def test_get_counters(self):
        self.assertEqual(self.client.get_counters("1152619"), [
            {"id": "1158038", "name": "14-007378", "type": "ХВС"},
            {"id": "1158039", "name": "14-087265", "type": "ГВС"},
        ])

    def test_get_device_info_maps_fields(self):
        info = self.client.get_device_info("1152619")
        self.assertEqual(set(info), {"1158038", "1158039"})
        cold = info["1158038"]
        self.assertEqual(cold["type"], "ХВС")
        self.assertEqual(cold["number"], "14-007378")
        self.assertEqual(cold["current_reading"], 483.0)
        self.assertEqual(cold["reading_period"], "2026-08-31")
        self.assertEqual(cold["inspection_date"], "2032-07-16")
        self.assertEqual(cold["inspection_status"], "OK")

    def test_readonly_inverts_enable_transfer(self):
        info = self.client.get_device_info("1152619")
        self.assertFalse(info["1158038"]["readonly"])   # enableTransfer: True
        self.assertTrue(info["1158039"]["readonly"])    # enableTransfer: False

    def test_unexpected_payload_raises(self):
        self.session.request.return_value = FakeResponse(200, {"data": None})
        with self.assertRaises(MosRuApiError):
            self.client.get_device_info("1152619")

    def test_missing_active_counters_is_empty(self):
        self.session.request.return_value = FakeResponse(200, {"data": [{"userPlaceId": 1}]})
        self.assertEqual(self.client.get_counters("1"), [])


# Ответ getInfo: квартиры лежат в data.addresses, flat приходит числом.
_PROFILE_PAYLOAD = {
    "data": {
        "user": {"nickName": "Олег К"},
        "addresses": [
            {"userPlaceId": 999001, "fls": "1111111111", "flat": 5},
            {"userPlaceId": 1152619, "fls": "1730249056", "flat": 218},
        ],
    }
}


class FindUserPlaceIdTest(unittest.TestCase):
    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session
        self.session.request.return_value = FakeResponse(200, _PROFILE_PAYLOAD)

    def test_finds_by_paycode_and_flat(self):
        self.assertEqual(self.client.find_user_place_id("1730249056", "218"), "1152619")

    def test_flat_compared_as_string(self):
        """flat в ответе — число, в конфиге строка: сравнение должно совпасть."""
        self.assertEqual(self.client.find_user_place_id("1730249056", 218), "1152619")

    def test_paycode_only(self):
        self.assertEqual(self.client.find_user_place_id("1111111111", ""), "999001")

    def test_wrong_flat_not_matched(self):
        with self.assertRaises(MosRuApiError):
            self.client.find_user_place_id("1730249056", "999")

    def test_unknown_paycode_raises(self):
        with self.assertRaises(MosRuApiError):
            self.client.find_user_place_id("0000000000", "")

    def test_empty_addresses(self):
        self.session.request.return_value = FakeResponse(200, {"data": {}})
        with self.assertRaises(MosRuApiError):
            self.client.find_user_place_id("1730249056", "218")


# Ответ getInfo с полями, которые нужны для выбора квартиры (ключи — из живого ответа).
_PLACES_PAYLOAD = {
    "data": {
        "addresses": [
            {"userPlaceId": 3395115, "fls": "1344364128", "flat": "46",
             "addressCaption": "ул. Тестовая, д. 1", "caption": "Дом"},
            {"userPlaceId": 999001, "fls": "1111111111", "flat": 5, "caption": "Дача"},
            # повтор той же квартиры
            {"userPlaceId": 3395115, "fls": "1344364128", "flat": "46"},
            # без userPlaceId — адресовать нельзя
            {"fls": "2222222222", "flat": "7"},
            {"userPlaceId": 777, "fls": None, "flat": None},
            "garbage",
        ],
    }
}


class ListPlacesTest(unittest.TestCase):
    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session
        self.session.request.return_value = FakeResponse(200, _PLACES_PAYLOAD)

    def test_requests_profile(self):
        self.client.list_places()
        method, url = self.session.request.call_args[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/profile/user/getInfo/"))

    def test_normalizes_and_deduplicates(self):
        self.assertEqual(self.client.list_places(), [
            {"user_place_id": "3395115", "paycode": "1344364128", "flat": "46",
             "address": "ул. Тестовая, д. 1"},
            {"user_place_id": "999001", "paycode": "1111111111", "flat": "5",
             "address": "Дача"},
            {"user_place_id": "777", "paycode": "", "flat": "", "address": ""},
        ])

    def test_empty_profile(self):
        self.session.request.return_value = FakeResponse(200, {"data": {}})
        self.assertEqual(self.client.list_places(), [])


_COLD = {"id": "1", "name": "14-007378", "type": "ХВС"}
_HOT = {"id": "2", "name": "14-087265", "type": "ГВС"}


class PickCountersTest(unittest.TestCase):
    def test_one_cold_one_hot(self):
        self.assertEqual(api.pick_counters([_HOT, _COLD]), ("1", "2"))

    def test_two_cold_meters_are_ambiguous(self):
        cold2 = {"id": "3", "name": "x", "type": "ХВС"}
        self.assertEqual(api.pick_counters([_COLD, cold2, _HOT]), (None, "2"))

    def test_unknown_types(self):
        self.assertEqual(
            api.pick_counters([{"id": "5", "name": "x", "type": ""},
                               {"id": "6", "name": "y", "type": "ЭЛ"}]),
            (None, None),
        )

    def test_empty(self):
        self.assertEqual(api.pick_counters([]), (None, None))

    def test_type_case_and_spaces(self):
        self.assertEqual(
            api.pick_counters([{"id": "1", "name": "a", "type": " хвс "},
                               {"id": "2", "name": "b", "type": "гвс"}]),
            ("1", "2"),
        )


class CountersOfTypeTest(unittest.TestCase):
    def test_filters_by_type(self):
        self.assertEqual(api.counters_of_type([_COLD, _HOT], api.COLD_TYPE), [_COLD])

    def test_falls_back_to_all_when_type_missing(self):
        other = {"id": "9", "name": "z", "type": ""}
        self.assertEqual(api.counters_of_type([other], api.HOT_TYPE), [other])


class PlaceLabelTest(unittest.TestCase):
    def test_full(self):
        self.assertEqual(
            api.place_label({"user_place_id": "1", "paycode": "1344364128",
                             "flat": "46", "address": "ул. Тестовая, д. 1"}),
            "ул. Тестовая, д. 1, кв. 46 — ЕПД 1344364128",
        )

    def test_without_address(self):
        self.assertEqual(
            api.place_label({"user_place_id": "1", "paycode": "1344364128",
                             "flat": "46", "address": ""}),
            "кв. 46 — ЕПД 1344364128",
        )

    def test_only_id(self):
        self.assertEqual(
            api.place_label({"user_place_id": "777", "paycode": "", "flat": "", "address": ""}),
            "777",
        )


class AuthorizeEdTest(unittest.TestCase):
    """OAuth ed.mos.ru: code из финального URL меняется на сессию."""

    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session

    def test_authorize_success(self):
        self.session.get.return_value = mock.Mock(
            url="https://ed.mos.ru/security/callback/sudir/login?code=ABC123")
        self.session.post.return_value = FakeResponse(200, {})

        self.client.authorize_ed()

        self.assertEqual(self.session.post.call_args.kwargs["params"], {"code": "ABC123"})
        self.assertIn("/profile/auth/web", self.session.post.call_args[0][0])

    def test_no_code_means_session_expired(self):
        # Сессия истекла: цепочка осталась на форме логина, code не выдан.
        self.session.get.return_value = mock.Mock(
            url="https://login.mos.ru/sps/login/methods/password?bo=%2Fsps")
        with self.assertRaises(MosRuAuthError):
            self.client.authorize_ed()
        self.session.post.assert_not_called()

    def test_auth_web_rejection(self):
        self.session.get.return_value = mock.Mock(
            url="https://ed.mos.ru/security/callback/sudir/login?code=ABC123")
        self.session.post.return_value = FakeResponse(403, {})
        with self.assertRaises(MosRuAuthError):
            self.client.authorize_ed()


class SessionNetworkTest(unittest.TestCase):
    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.client._session = self.session

    def test_probe_timeout_is_temporary(self):
        self.session.get.side_effect = api.requests.Timeout()
        with self.assertRaises(MosRuTemporaryError): self.client.try_refresh_acst()

    def test_probe_503_is_temporary(self):
        self.session.get.return_value = mock.Mock(status_code=503, url="https://www.mos.ru/error")
        with self.assertRaises(MosRuTemporaryError): self.client.try_refresh_acst()

    def test_oauth_503_does_not_request_qr(self):
        self.session.get.return_value = mock.Mock(status_code=503, url="https://login.mos.ru/error")
        with self.assertRaises(MosRuTemporaryError): self.client.authorize_ed()
        self.session.post.assert_not_called()

    def test_auth_503_is_temporary(self):
        self.session.get.return_value = mock.Mock(status_code=200,
            url="https://ed.mos.ru/security/callback/sudir/login?code=test")
        self.session.post.return_value = FakeResponse(503, {})
        with self.assertRaises(MosRuTemporaryError): self.client.authorize_ed()


_TOTP_URL = "https://login.mos.ru/sps/login/methods2/totp?bo=%2Fsps%2Foauth%2Fae"
_TRUST_URL = "https://login.mos.ru/sps/login/ur/askToTrust?bo=%2Fsps%2Foauth%2Fae"
_SATISFY_URL = "https://www.mos.ru/api/acs/v1/login/satisfy?code=test"


def _page(status: int, url: str, *, location: str | None = None, text: str = ""):
    """Ответ login.mos.ru: для редиректов — Location, для страниц — HTML."""
    return mock.Mock(
        status_code=status, url=url, text=text, history=[],
        headers={"Location": location} if location else {},
    )


class QrSecondFactorTest(unittest.TestCase):
    """После сканирования QR mos.ru может запросить второй фактор.

    Цепочка из реального входа аккаунта с приложением-аутентификатором:
    POST qrCode/complete → 303 → /sps/login/methods2/totp. Пока код не введён,
    SSO-сессии нет, и вход в ed.mos.ru уходит на форму пароля.
    """

    def setUp(self):
        self.client = MosRuClient()
        self.session = mock.Mock()
        self.session.cookies = []
        self.client._session = self.session

    def test_totp_page_requires_code(self):
        self.session.post.return_value = _page(200, _TOTP_URL)
        self.assertEqual(self.client.complete_qr_auth(), "totp_required")

    def test_leaving_login_host_is_done(self):
        self.session.post.return_value = _page(200, "https://www.mos.ru/")
        self.assertEqual(self.client.complete_qr_auth(), "done")

    def test_unknown_login_step_is_not_reported_as_done(self):
        # Раньше любой неизвестный шаг считался успехом, а ошибка всплывала
        # позже как «сессия истекла».
        self.session.post.return_value = _page(
            200, "https://login.mos.ru/sps/login/methods/password?bo=%2Fsps")
        with self.assertRaises(MosRuAuthError) as ctx:
            self.client.complete_qr_auth()
        self.assertIn("/sps/login/methods/password", str(ctx.exception))

    def _start_totp(self):
        self.session.post.return_value = _page(200, _TOTP_URL)
        self.client.complete_qr_auth()
        self.session.reset_mock()

    def test_totp_code_is_posted_to_totp_page(self):
        self._start_totp()
        self.session.post.return_value = _page(303, _TOTP_URL, location=_SATISFY_URL)
        self.session.get.side_effect = [
            _page(302, _SATISFY_URL, location="https://www.mos.ru/"),
            _page(200, "https://www.mos.ru/"),
        ]

        self.client.submit_totp("123456")

        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], _TOTP_URL)
        self.assertEqual(kwargs["data"], {"otp": "123456"})
        self.assertFalse(kwargs["allow_redirects"])
        # satisfy требует навигационных заголовков, как у браузера.
        first_hop = self.session.get.call_args_list[0]
        self.assertEqual(first_hop.args[0], _SATISFY_URL)
        self.assertEqual(first_hop.kwargs["headers"]["Sec-Fetch-Mode"], "navigate")

    def test_wrong_totp_code_stays_on_totp_page(self):
        self._start_totp()
        self.session.post.return_value = _page(200, _TOTP_URL)
        with self.assertRaises(MosRuAuthError):
            self.client.submit_totp("000000")

    def test_totp_then_trust_device(self):
        self._start_totp()
        trust_form = (
            '<form action="/sps/login/ur/askToTrust?bo=%2Fsps" method="post">'
            '<input type="hidden" name="csrf" value="tok"></form>'
        )
        self.session.post.side_effect = [
            _page(303, _TOTP_URL, location=_TRUST_URL),
            _page(302, _TRUST_URL, location="https://www.mos.ru/"),
        ]
        self.session.get.side_effect = [
            _page(200, _TRUST_URL, text=trust_form),
            _page(200, "https://www.mos.ru/"),
        ]
        self.session.cookies = [mock.Mock(name="cookie")]
        self.session.cookies[0].name = "Ltpatoken2"

        self.client.submit_totp("123456")

        trust_call = self.session.post.call_args_list[1]
        self.assertEqual(trust_call.kwargs["data"], {"csrf": "tok", "action": "trust"})

    def test_totp_without_pending_login_fails(self):
        with self.assertRaises(MosRuAuthError):
            self.client.submit_totp("123456")
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
