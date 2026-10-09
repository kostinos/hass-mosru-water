"""Isolated flow regression tests; execute actual methods without installing HA."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/mosru_water/config_flow.py"

_API_PATH = SOURCE.parent / "api.py"
_spec = importlib.util.spec_from_file_location("mosru_water_api_for_flow", _API_PATH)
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def load_flow_methods():
    # Keep the actual method bodies, replacing only the unavailable HA base class
    # and imports. These tests cover our transitions, not HA's UI/framework.
    tree = ast.parse(SOURCE.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    names = {"__init__", "async_step_reauth", "_notify_qr_auth", "_poll_qr_scan",
             "async_step_qr", "async_step_code", "async_step_totp", "_async_submit_code",
             "async_step_user", "async_step_place", "_load_places", "_async_select_place",
             "async_step_discover"}
    names.update({'_async_cleanup_qr', '_qr_finished'})
    cls.bases = []
    cls.keywords = []
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    required_calls = []

    def required(key, **kw):
        required_calls.append((key, kw))
        return key

    namespace = {
        "_vol_required": required_calls,
        "_AUTH_SETTINGS_URL": "/config/integrations/integration/mosru_water",
        "vol": SimpleNamespace(Schema=lambda value: value,
                               Required=required,
                               Optional=lambda key, **kw: key),
        "CONF_PAYCODE": "paycode", "CONF_FLAT": "flat",
        "CONF_USER_PLACE_ID": "user_place_id",
        "CONF_COLD_ID": "cold_counter_id", "CONF_HOT_ID": "hot_counter_id",
        "pick_counters": api.pick_counters,
        "counters_of_type": api.counters_of_type,
        "place_label": api.place_label,
        "COLD_TYPE": api.COLD_TYPE, "HOT_TYPE": api.HOT_TYPE,
        "selector": Mock(),
        "_LOGGER": Mock(),
        "MosRuClient": Mock(),
        "CONF_SESSION_COOKIES": "session_cookies",
        "MosRuAuthError": type("MosRuAuthError", (Exception,), {}),
        "MosRuApiError": type("MosRuApiError", (Exception,), {}),
        "pn_create": Mock(), "pn_dismiss": Mock(),
        "time": SimpleNamespace(time=lambda: 123),
        "asyncio": SimpleNamespace(sleep=AsyncMock()),
        "_QR_POLL_SECONDS": 2,
        "_write_qr_svg": Mock(return_value="/local/mosru_water_qr.svg?t=123"),
        "_delete_qr_svg": Mock(),
    }
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace["MosRuWaterConfigFlow"], namespace


class ReauthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cls, self.ns = load_flow_methods()
        self.flow = cls()
        self.entry = SimpleNamespace(data={"session_cookies": {"old": "cookie"},
                                          "paycode": "123"})
        async def executor(fn, *args):
            return fn(*args)
        self.flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=self.entry)),
            async_add_executor_job=executor,
            config=SimpleNamespace(path=Mock(return_value="/tmp/www")),
        )
        self.flow.context = {"entry_id": "entry"}
        self.flow.async_show_form = Mock(return_value={"type": "form"})
        self.flow.async_show_progress = Mock(return_value={"type": "progress"})
        self.flow.async_update_reload_and_abort = Mock(return_value={"type": "abort"})
        self.flow.async_abort = Mock()
        self.flow._abort_if_in_progress = Mock()

    async def test_reauth_starts_session_for_notification_link(self):
        self.flow.async_step_qr = AsyncMock()
        await self.flow.async_step_reauth()
        self.ns["MosRuClient"].assert_called_once()
        self.flow.async_step_qr.assert_awaited_once()
        self.assertIs(self.flow._reauth_entry, self.entry)

    async def test_notification_links_to_current_mosru_session(self):
        client = Mock()
        client.start_qr_session.return_value = {"link": "https://login.mos.ru/qr/mm/?code=test-session"}
        self.flow._client = client
        task = Mock()
        task.done.return_value = False
        self.flow._poll_qr_scan = Mock(return_value="poll")
        self.flow.hass.async_create_task = Mock(return_value=task)
        await self.flow.async_step_qr()
        message = self.ns["pn_create"].call_args.kwargs["message"]
        self.assertIn("[Подтвердить вход](https://login.mos.ru/qr/mm/?code=test-session)", message)
        self.assertNotIn("/config/integrations", message)
        placeholders = self.flow.async_show_progress.call_args.kwargs["description_placeholders"]
        for name in ("strings.json", "translations/ru.json", "translations/en.json"):
            with self.subTest(translation=name):
                template = json.loads((SOURCE.parent / name).read_text())["config"]["progress"]["scanning"]
                rendered = template.format(**placeholders)
                self.assertIn("](https://login.mos.ru/qr/mm/?code=test-session)", rendered)
                self.assertIn("](/local/mosru_water_qr.svg?t=123)", rendered)
                self.assertNotIn("вкладке](/local/", rendered)
                self.assertNotIn("tab](/local/", rendered)

    async def test_refresh_updates_notification_link(self):
        self.flow._client = Mock()
        self.flow._client.poll_qr.side_effect = ["needRefresh", "needComplete"]
        self.flow._client.refresh_qr.return_value = {
            "link": "https://login.mos.ru/qr/mm/?code=new-session"}
        self.flow._client.complete_qr_auth.return_value = "done"
        self.flow._qr_link = "https://login.mos.ru/qr/mm/?code=old-session"
        await self.flow._poll_qr_scan()
        message = self.ns["pn_create"].call_args.kwargs["message"]
        self.assertIn("?code=new-session", message)
        self.assertNotIn("?code=old-session", message)

    async def test_2fa_notification_keeps_link_to_continue_authorization(self):
        self.flow._qr_task = asyncio.get_running_loop().create_future()
        self.flow._qr_task.set_result("code_required")
        self.flow.async_show_progress_done = Mock()
        await self.flow.async_step_qr()
        self.flow.async_show_progress_done.assert_called_once_with(next_step_id="code")
        self.ns["pn_dismiss"].assert_not_called()
        self.assertIn(
            "[Продолжить авторизацию](/config/integrations/integration/mosru_water)",
            self.ns["pn_create"].call_args.kwargs["message"],
        )

    async def test_qr_success_updates_and_reloads_existing_entry(self):
        self.flow._reauth_entry = self.entry
        self.flow._client = Mock()
        self.flow._client.get_session_cookies.return_value = {"new": "cookie"}
        self.flow._qr_task = asyncio.get_running_loop().create_future()
        self.flow._qr_task.set_result(True)
        await self.flow.async_step_qr()
        self.flow.async_update_reload_and_abort.assert_called_once_with(
            self.entry, data_updates={"session_cookies": {"new": "cookie"}})

    async def test_2fa_success_updates_and_reloads_existing_entry(self):
        self.flow._reauth_entry = self.entry
        self.flow._client = Mock()
        self.flow._client.get_session_cookies.return_value = {"new": "cookie"}
        await self.flow.async_step_code({"sms_code": "123456"})
        self.ns["pn_dismiss"].assert_called_once_with(
            self.flow.hass, notification_id="mosru_water_qr")
        self.flow.async_update_reload_and_abort.assert_called_once_with(
            self.entry, data_updates={"session_cookies": {"new": "cookie"}})


class TotpFlowTest(unittest.IsolatedAsyncioTestCase):
    """Аккаунт с приложением-аутентификатором: после QR нужен TOTP-код."""

    setUp = ReauthTest.setUp

    async def test_poll_reports_totp(self):
        self.flow._client = Mock()
        self.flow._client.poll_qr.return_value = "needComplete"
        self.flow._client.complete_qr_auth.return_value = "totp_required"
        self.assertEqual(await self.flow._poll_qr_scan(), "totp_required")

    async def test_qr_continues_to_totp_step(self):
        self.flow._qr_task = asyncio.get_running_loop().create_future()
        self.flow._qr_task.set_result("totp_required")
        self.flow.async_show_progress_done = Mock()
        await self.flow.async_step_qr()
        self.flow.async_show_progress_done.assert_called_once_with(next_step_id="totp")
        message = self.ns["pn_create"].call_args.kwargs["message"]
        self.assertIn("аутентификатор", message)
        self.assertIn("(/config/integrations/integration/mosru_water)", message)

    async def test_totp_success_updates_and_reloads_existing_entry(self):
        self.flow._reauth_entry = self.entry
        self.flow._client = Mock()
        self.flow._client.get_session_cookies.return_value = {"new": "cookie"}
        await self.flow.async_step_totp({"sms_code": " 123456 "})
        self.flow._client.submit_totp.assert_called_once_with("123456")
        self.flow._client.submit_sms_and_trust.assert_not_called()
        self.flow.async_update_reload_and_abort.assert_called_once_with(
            self.entry, data_updates={"session_cookies": {"new": "cookie"}})

    async def test_wrong_totp_code_asks_again(self):
        self.flow._client = Mock()
        self.flow._client.submit_totp.side_effect = self.ns["MosRuAuthError"]()
        await self.flow.async_step_totp({"sms_code": "000000"})
        kwargs = self.flow.async_show_form.call_args.kwargs
        self.assertEqual(kwargs["step_id"], "totp")
        self.assertEqual(kwargs["errors"], {"sms_code": "invalid_code"})

    async def test_unfinished_login_aborts_instead_of_new_qr(self):
        self.flow._client = Mock()
        self.flow._client.poll_qr.return_value = "needComplete"
        self.flow._client.complete_qr_auth.side_effect = self.ns["MosRuAuthError"]()
        self.assertEqual(await self.flow._poll_qr_scan(), "login_incomplete")

        self.flow._qr_task = asyncio.get_running_loop().create_future()
        self.flow._qr_task.set_result("login_incomplete")
        await self.flow.async_step_qr()
        self.flow.async_abort.assert_called_once_with(reason="login_incomplete")

    def test_translations_describe_totp_step(self):
        for name in ("strings.json", "translations/ru.json", "translations/en.json"):
            with self.subTest(translation=name):
                config = json.loads((SOURCE.parent / name).read_text())["config"]
                self.assertIn("sms_code", config["step"]["totp"]["data"])
                self.assertIn("login_incomplete", config["abort"])
