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


if __name__ == "__main__":
    unittest.main()
