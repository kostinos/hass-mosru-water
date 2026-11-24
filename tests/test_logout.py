"""Logout transitions without a running Home Assistant instance."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_reauth import load_flow_methods

SOURCE = Path(__file__).resolve().parents[1] / 'custom_components/mosru_water/config_flow.py'


def load_logout_methods():
    tree = ast.parse(SOURCE.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'MosRuWaterOptionsFlow')
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in {'__init__', 'async_step_init', 'async_step_logout',
                               'async_step_additional_meters', 'async_step_settings'}]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    _, ns = load_flow_methods()
    ns['DOMAIN'] = 'mosru_water'
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), ns)
    return ns['MosRuWaterOptionsFlow']


class LogoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = {'session_cookies': {'old': 'cookie'}, 'user_place_id': 'old-profile',
                         'paycode': '123', 'cold_counter_id': 'cold', 'submit_day': 20}
        self.entry = SimpleNamespace(entry_id='entry', data=dict(self.original),
                                     options={'cold_entity': 'sensor.water'}, async_start_reauth=Mock())
        self.flow = load_logout_methods()(self.entry)
        self.manager = SimpleNamespace(async_unload=AsyncMock(return_value=True),
                                       async_update_entry=Mock())
        self.flow.hass = SimpleNamespace(config_entries=self.manager)
        self.flow.async_show_form = Mock()
        self.flow.async_show_menu = Mock()
        self.flow.async_abort = Mock()

    async def test_opening_confirmation_does_not_log_out(self):
        await self.flow.async_step_logout()
        self.manager.async_unload.assert_not_called()
        self.manager.async_update_entry.assert_not_called()
        self.entry.async_start_reauth.assert_not_called()

    async def test_logout_stops_client_clears_session_and_preserves_settings(self):
        events = []
        self.manager.async_unload.side_effect = lambda *a: events.append('unload') or True
        self.manager.async_update_entry.side_effect = lambda *a, **k: events.append('clear')
        self.entry.async_start_reauth.side_effect = lambda *a: events.append('reauth')
        await self.flow.async_step_logout({})
        self.assertEqual(events, ['unload', 'clear', 'reauth'])
        data = self.manager.async_update_entry.call_args.kwargs['data']
        self.assertEqual(data, {'session_cookies': {}, 'paycode': '123',
                                'cold_counter_id': 'cold', 'submit_day': 20})
        self.assertEqual(self.entry.data, self.original)
        self.assertEqual(self.entry.options, {'cold_entity': 'sensor.water'})
        self.entry.async_start_reauth.assert_called_once_with(self.flow.hass)

    async def test_unload_failure_keeps_session_and_does_not_start_reauth(self):
        self.manager.async_unload.return_value = False
        await self.flow.async_step_logout({})
        self.manager.async_update_entry.assert_not_called()
        self.entry.async_start_reauth.assert_not_called()
        self.flow.async_abort.assert_called_once_with(reason='logout_failed')

    async def test_menu_exposes_settings_and_logout(self):
        await self.flow.async_step_init()
        self.flow.async_show_menu.assert_called_once_with(
            step_id='init', menu_options=['settings', 'additional_meters', 'logout'])


class AdditionalMeterOptionsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = SimpleNamespace(
            entry_id="entry",
            data=dict(cold_counter_id="1", hot_counter_id="2",
                      cold_entity="sensor.one", hot_entity="sensor.two"),
            options={"submit_day": 18},
        )
        self.flow = load_logout_methods()(self.entry)
        coordinator = SimpleNamespace(data={"available_meters": {str(i): {} for i in range(1, 5)}})
        self.flow.hass = SimpleNamespace(data={"mosru_water": {"entry": coordinator}})
        self.flow.async_show_form = Mock()
        self.flow.async_create_entry = Mock()

    async def test_add_change_remove_through_existing_entry_options(self):
        for meter, sensor in [("3", "sensor.three"), ("4", "sensor.four"),
                              ("3", "sensor.replacement"), ("4", None)]:
            fields = {"meter_id": meter}
            if sensor:
                fields["meter_entity"] = sensor
            await self.flow.async_step_additional_meters(fields)
            self.entry.options = self.flow.async_create_entry.call_args.kwargs["data"]
        self.assertEqual(self.entry.options, {
            "submit_day": 18, "extra_meters": {"3": "sensor.replacement"},
        })
        self.assertEqual(self.entry.data["cold_entity"], "sensor.one")

    async def test_primary_bindings_cannot_be_reused_for_extra_meter(self):
        await self.flow.async_step_additional_meters({
            "meter_id": "1", "meter_entity": "sensor.three",
        })
        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"],
                         {"meter_id": "unknown_meter"})
        await self.flow.async_step_additional_meters({
            "meter_id": "3", "meter_entity": "sensor.one",
        })
        self.assertEqual(self.flow.async_show_form.call_args.kwargs["errors"],
                         {"meter_entity": "duplicate_sensors"})
        self.flow.async_create_entry.assert_not_called()

    async def test_editing_primary_settings_preserves_additional_meters(self):
        extra = {"3": "sensor.three"}
        self.entry.options["extra_meters"] = extra
        await self.flow.async_step_settings({**self.entry.data, "submit_day": 19})
        self.assertEqual(self.flow.async_create_entry.call_args.kwargs["data"]["extra_meters"], extra)
