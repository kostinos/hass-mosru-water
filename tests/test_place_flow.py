"""Мастер: квартира из профиля «Электронного дома», счётчики по типу ХВС/ГВС."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_reauth import SOURCE, identity, load_flow_methods

_PLACE_A = {"user_place_id": "3395115", "paycode": "1344364128", "flat": "46",
            "address": "ул. Тестовая, д. 1"}
_PLACE_B = {"user_place_id": "999001", "paycode": "1111111111", "flat": "5",
            "address": "Дача"}
_COLD = {"id": "1", "name": "TEST-COLD-001", "type": "ХВС"}
_HOT = {"id": "2", "name": "TEST-HOT-001", "type": "ГВС"}


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
        # Native unique-ID handling is mocked; legacy matching reads real fixtures.
        self.flow.async_set_unique_id = AsyncMock()
        self.flow._abort_if_unique_id_configured = Mock()
        self.flow._async_current_entries = Mock(return_value=[])

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

    async def test_another_flat_can_be_added(self):
        # Вторая квартира, в том числе на другом аккаунте mos.ru.
        self.flow._async_current_entries = Mock(return_value=["entry"])
        self.flow.async_step_qr = AsyncMock(return_value={"type": "progress"})
        await self.flow.async_step_user()
        self.flow.async_step_qr.assert_awaited_once()
        self.flow.async_abort.assert_not_called()

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

    async def test_place_becomes_unique_id(self):
        self.flow._client.list_places.return_value = [_PLACE_A]
        await self.flow.async_step_place()
        self.flow.async_set_unique_id.assert_awaited_once_with(
            'flat:["1344364128","46"]')
        self.flow._abort_if_unique_id_configured.assert_called_once()
        self.flow._async_current_entries.assert_called_once_with(include_ignore=False)

    async def test_already_added_place_stops_wizard(self):
        self.flow._client.list_places.return_value = [_PLACE_A]
        self.flow._async_current_entries.return_value = [
            SimpleNamespace(data=dict(_PLACE_A), unique_id=None)
        ]
        await self.flow.async_step_place()
        self.flow.async_abort.assert_called_once_with(reason="already_configured")
        self.assertEqual(self.flow._data, {})
        self.flow.async_step_discover.assert_not_awaited()

    async def test_logged_out_legacy_flat_with_changed_profile_is_rejected(self):
        self.flow._async_current_entries.return_value = [SimpleNamespace(
            data={"paycode": "1344364128", "flat": 46, "session_cookies": {}},
            unique_id=None, disabled_by="user")]
        await self.flow._async_select_place(
            _PLACE_A | {"user_place_id": "new-profile"})
        self.flow.async_abort.assert_called_once_with(reason="already_configured")
        self.flow.async_step_discover.assert_not_awaited()
        self.assertEqual(self.flow._data, {})

    async def test_same_flat_on_another_account_is_rejected(self):
        self.flow._async_current_entries.return_value = [SimpleNamespace(
            data=_PLACE_A | {"user_place_id": "another-account-profile"},
            unique_id="another-account-profile")]
        await self.flow._async_select_place(_PLACE_A)
        self.flow.async_abort.assert_called_once_with(reason="already_configured")

    async def test_another_flat_can_finish_selection(self):
        self.flow._async_current_entries.return_value = [SimpleNamespace(
            data=_PLACE_A, unique_id=identity.place_unique_id(_PLACE_A))]
        await self.flow._async_select_place(_PLACE_B)
        self.flow.async_abort.assert_not_called()
        self.flow.async_step_discover.assert_awaited_once()

    async def test_missing_billing_details_use_profile_fallback(self):
        place = _PLACE_B | {"paycode": "", "flat": ""}
        await self.flow._async_select_place(place)
        self.flow.async_set_unique_id.assert_awaited_once_with("999001")
        self.flow.async_step_discover.assert_awaited_once()

    async def test_logged_out_profile_fallback_matches_legacy_unique_id(self):
        self.flow._async_current_entries.return_value = [
            SimpleNamespace(data={"session_cookies": {}}, unique_id="999001")]
        await self.flow._async_select_place(
            _PLACE_B | {"paycode": "", "flat": ""})
        self.flow.async_abort.assert_called_once_with(reason="already_configured")

    async def test_empty_legacy_record_does_not_match_every_flat(self):
        self.flow._async_current_entries.return_value = [
            SimpleNamespace(data={"paycode": "", "flat": ""}, unique_id=None)]
        await self.flow._async_select_place(
            _PLACE_B | {"paycode": "", "flat": ""})
        self.flow.async_abort.assert_not_called()
        self.flow.async_step_discover.assert_awaited_once()

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


class EntryTitleTest(FlowTestBase):
    async def test_saved_entries_have_distinguishable_names(self):
        del self.flow.async_step_sensors
        self.flow.async_create_entry = Mock()
        titles = []
        for place in (_PLACE_A, _PLACE_B):
            self.flow._data = dict(place)
            await self.flow.async_step_sensors({
                "cold_entity": "sensor.cold",
                "hot_entity": "sensor.hot",
                "submit_day": 20,
            })
            call = self.flow.async_create_entry.call_args.kwargs
            titles.append(call["title"])
            self.assertIn(place["paycode"], call["title"])
            self.assertIn(place["flat"], call["title"])
        self.assertNotEqual(*titles)


class TranslationsTest(unittest.TestCase):
    def test_place_step_and_reasons(self):
        for name in ("strings.json", "translations/ru.json", "translations/en.json"):
            with self.subTest(translation=name):
                config = json.loads((SOURCE.parent / name).read_text())["config"]
                self.assertNotIn("user", config["step"])
                self.assertIn("user_place_id", config["step"]["place"]["data"])
                self.assertIn("{error}", config["error"]["cannot_get_places"])
                self.assertIn("no_places", config["abort"])

class DuplicateMeterTest(FlowTestBase):
    async def test_dropdown_duplicate_keeps_wizard_on_discover(self):
        self.flow._counters_fetched = True
        self.flow._counters = [{'id': 'same', 'name': 'test', 'type': ''}]
        self.flow._suggested_counters = (None, None)
        await self.flow.async_step_discover({'cold_counter_id': 'same', 'hot_counter_id': 'same'})
        self.flow.async_step_sensors.assert_not_awaited()
        self.assertEqual(self.form_kwargs()['errors']['base'], 'duplicate_counters')

    async def test_manual_duplicate_keeps_wizard_on_discover(self):
        self.flow._counters_fetched = True
        await self.flow.async_step_discover({'cold_counter_id': 'same', 'hot_counter_id': 'same'})
        self.flow.async_step_sensors.assert_not_awaited()
        self.assertEqual(self.form_kwargs()['errors']['base'], 'duplicate_counters')
