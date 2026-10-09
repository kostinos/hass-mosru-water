"""Мастер: квартира из профиля «Электронного дома», счётчики по типу ХВС/ГВС."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_reauth import SOURCE, load_flow_methods

_PLACE_A = {"user_place_id": "3395115", "paycode": "1344364128", "flat": "46",
            "address": "ул. Тестовая, д. 1"}
_PLACE_B = {"user_place_id": "999001", "paycode": "1111111111", "flat": "5",
            "address": "Дача"}
_COLD = {"id": "1", "name": "14-007378", "type": "ХВС"}
_HOT = {"id": "2", "name": "14-087265", "type": "ГВС"}


class FlowTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cls, self.ns = load_flow_methods()
        self.flow = cls()

        async def executor(fn, *args):
            return fn(*args)

        self.flow.hass = SimpleNamespace(
            async_add_executor_job=executor,
            config=SimpleNamespace(path=Mock(return_value="/tmp/www")),
        )
        self.flow._client = Mock()
        self.flow.async_show_form = Mock(return_value={"type": "form"})
        self.flow.async_abort = Mock(return_value={"type": "abort"})
        self.flow.async_step_sensors = AsyncMock(return_value={"type": "sensors"})

    def form_kwargs(self):
        return self.flow.async_show_form.call_args.kwargs


class UserStepTest(FlowTestBase):
    async def test_goes_straight_to_qr(self):
        self.flow._async_current_entries = Mock(return_value=[])
        self.flow.async_step_qr = AsyncMock(return_value={"type": "progress"})
        await self.flow.async_step_user()
        self.ns["MosRuClient"].assert_called_once()
        self.flow.async_step_qr.assert_awaited_once()
        self.flow.async_show_form.assert_not_called()

    async def test_single_entry_only(self):
        self.flow._async_current_entries = Mock(return_value=["entry"])
        await self.flow.async_step_user()
        self.flow.async_abort.assert_called_once_with(reason="already_configured")

    async def test_qr_success_leads_to_place(self):
        self.flow._client.get_session_cookies.return_value = {"c": "v"}
        self.flow._qr_task = asyncio.get_running_loop().create_future()
        self.flow._qr_task.set_result(True)
        self.flow.async_show_progress_done = Mock()
        await self.flow.async_step_qr()
        self.flow.async_show_progress_done.assert_called_once_with(next_step_id="place")

    async def test_code_success_leads_to_place(self):
        self.flow.async_step_place = AsyncMock(return_value={"type": "place"})
        await self.flow.async_step_totp({"sms_code": "123456"})
        self.flow.async_step_place.assert_awaited_once()

    async def test_sms_code_success_leads_to_place(self):
        self.flow.async_step_place = AsyncMock(return_value={"type": "place"})
        await self.flow.async_step_code({"sms_code": "123456"})
        self.flow.async_step_place.assert_awaited_once()


class PlaceStepTest(FlowTestBase):
    def setUp(self):
        super().setUp()
        self.flow.async_step_discover = AsyncMock(return_value={"type": "discover"})

    async def test_single_place_is_selected_without_form(self):
        self.flow._client.list_places.return_value = [_PLACE_A]
        await self.flow.async_step_place()
        self.flow._client.authorize_ed.assert_called_once()
        self.flow.async_show_form.assert_not_called()
        self.assertEqual(self.flow._data, {
            "user_place_id": "3395115", "paycode": "1344364128", "flat": "46"})
        self.flow.async_step_discover.assert_awaited_once()

    async def test_several_places_show_choice(self):
        self.flow._client.list_places.return_value = [_PLACE_A, _PLACE_B]
        selector = self.ns["selector"]
        await self.flow.async_step_place()
        self.assertEqual(self.form_kwargs()["step_id"], "place")
        options = selector.SelectSelectorConfig.call_args.kwargs["options"]
        self.assertEqual(len(options), 2)
        labels = [c.kwargs["label"] for c in selector.SelectOptionDict.call_args_list]
        self.assertIn("ул. Тестовая, д. 1, кв. 46 — ЕПД 1344364128", labels)
        self.flow.async_step_discover.assert_not_awaited()

    async def test_choice_is_saved(self):
        self.flow._client.list_places.return_value = [_PLACE_A, _PLACE_B]
        await self.flow.async_step_place()
        await self.flow.async_step_place({"user_place_id": "999001"})
        self.assertEqual(self.flow._data, {
            "user_place_id": "999001", "paycode": "1111111111", "flat": "5"})
        self.flow.async_step_discover.assert_awaited_once()
        self.flow._client.list_places.assert_called_once()

    async def test_unknown_place_choice_reshows_form(self):
        self.flow._client.list_places.return_value = [_PLACE_A, _PLACE_B]
        await self.flow.async_step_place()
        await self.flow.async_step_place({"user_place_id": "nope"})
        self.assertEqual(self.form_kwargs()["step_id"], "place")
        self.assertEqual(self.flow.async_show_form.call_count, 2)
        self.flow.async_step_discover.assert_not_awaited()
        self.flow._client.list_places.assert_called_once()

    async def test_no_places_aborts(self):
        self.flow._client.list_places.return_value = []
        await self.flow.async_step_place()
        self.flow.async_abort.assert_called_once_with(reason="no_places")

    async def test_rejected_session_aborts(self):
        self.flow._client.authorize_ed.side_effect = self.ns["MosRuAuthError"]()
        await self.flow.async_step_place()
        self.flow.async_abort.assert_called_once_with(reason="session_expired")

    async def test_api_error_shows_reason_and_retries(self):
        self.flow._client.list_places.side_effect = [
            self.ns["MosRuApiError"]("ed.mos.ru auth: HTTP 451"), [_PLACE_A]]
        await self.flow.async_step_place()
        kwargs = self.form_kwargs()
        self.assertEqual(kwargs["errors"], {"base": "cannot_get_places"})
        self.assertEqual(kwargs["description_placeholders"],
                         {"error": "ed.mos.ru auth: HTTP 451"})
        await self.flow.async_step_place({})
        self.flow.async_step_discover.assert_awaited_once()
        self.assertEqual(self.flow._data["user_place_id"], "3395115")


class DiscoverStepTest(FlowTestBase):
    def setUp(self):
        super().setUp()
        self.flow._data = {"user_place_id": "3395115"}

    async def test_cold_and_hot_are_assigned_automatically(self):
        self.flow._client.get_counters.return_value = [_HOT, _COLD]
        await self.flow.async_step_discover()
        self.flow._client.get_counters.assert_called_once_with("3395115")
        self.assertEqual(self.flow._data["cold_counter_id"], "1")
        self.assertEqual(self.flow._data["hot_counter_id"], "2")
        self.flow.async_show_form.assert_not_called()
        self.flow.async_step_sensors.assert_awaited_once()

    async def test_ambiguous_meters_are_filtered_and_prefilled(self):
        cold2 = {"id": "3", "name": "14-000001", "type": "ХВС"}
        self.flow._client.get_counters.return_value = [_COLD, cold2, _HOT]
        selector = self.ns["selector"]
        await self.flow.async_step_discover()
        self.assertEqual(self.form_kwargs()["step_id"], "discover")
        offered = {c.kwargs["value"] for c in selector.SelectOptionDict.call_args_list}
        self.assertEqual(offered, {"1", "2", "3"})
        sizes = sorted(len(c.kwargs["options"])
                       for c in selector.SelectSelectorConfig.call_args_list)
        self.assertEqual(sizes, [1, 2])  # ГВС: один, ХВС: два
        self.assertEqual(self.flow._suggested_counters, (None, "2"))
        required = dict(self.ns["_vol_required"])
        self.assertEqual(required["hot_counter_id"],
                         {"description": {"suggested_value": "2"}})
        self.assertEqual(required["cold_counter_id"], {})

    async def test_retry_discovery_recovers_automatic_assignment(self):
        self.flow._client.get_counters.side_effect = self.ns["MosRuApiError"]()
        await self.flow.async_step_discover()
        self.flow._client.get_counters.side_effect = None
        self.flow._client.get_counters.return_value = [_COLD, _HOT]
        await self.flow.async_step_discover({"retry_discovery": True})
        self.assertEqual(self.flow._data["cold_counter_id"], "1")
        self.assertEqual(self.flow._data["hot_counter_id"], "2")
        self.flow.async_step_sensors.assert_awaited_once()

    async def test_user_choice_is_saved(self):
        self.flow._client.get_counters.return_value = [_COLD, _COLD | {"id": "3"}, _HOT]
        await self.flow.async_step_discover()
        await self.flow.async_step_discover(
            {"cold_counter_id": "3", "hot_counter_id": "2"})
        self.assertEqual(self.flow._data["cold_counter_id"], "3")
        self.flow.async_step_sensors.assert_awaited_once()

    async def test_api_error_falls_back_to_manual_ids(self):
        self.flow._client.get_counters.side_effect = self.ns["MosRuApiError"]()
        await self.flow.async_step_discover()
        self.assertEqual(self.form_kwargs()["step_id"], "discover")
        self.assertIn("Введите ID вручную",
                      self.form_kwargs()["description_placeholders"]["description"])


class TranslationsTest(unittest.TestCase):
    def test_place_step_and_reasons(self):
        for name in ("strings.json", "translations/ru.json", "translations/en.json"):
            with self.subTest(translation=name):
                config = json.loads((SOURCE.parent / name).read_text())["config"]
                self.assertNotIn("user", config["step"])
                self.assertIn("user_place_id", config["step"]["place"]["data"])
                self.assertIn("{error}", config["error"]["cannot_get_places"])
                self.assertIn("no_places", config["abort"])
