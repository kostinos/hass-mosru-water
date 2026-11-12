"""Stable apartment identities and in-place migrations; no portal requests."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_logout import load_logout_methods
from test_reauth import SOURCE, identity, load_flow_methods


class PlaceIdentityTest(unittest.TestCase):
    def test_identity_survives_logout_and_profile_change(self):
        data = {"paycode": "001234", "flat": "46", "user_place_id": "old"}
        expected = 'flat:["001234","46"]'
        self.assertEqual(identity.place_unique_id(data), expected)
        self.assertEqual(
            identity.place_unique_id(data | {"user_place_id": "new"}), expected
        )
        data.pop("user_place_id")
        self.assertEqual(identity.place_unique_id(data), expected)

    def test_string_number_and_whitespace_normalization(self):
        self.assertEqual(
            identity.place_unique_id({"paycode": " 123 ", "flat": 46}),
            identity.place_unique_id({"paycode": 123, "flat": "46"}),
        )
        self.assertNotEqual(
            identity.place_unique_id({"paycode": "00123", "flat": "46"}),
            identity.place_unique_id({"paycode": "123", "flat": "46"}),
        )

    def test_separators_do_not_make_ambiguous_ids(self):
        self.assertNotEqual(
            identity.place_unique_id({"paycode": "a:b", "flat": "c"}),
            identity.place_unique_id({"paycode": "a", "flat": "b:c"}),
        )

    def test_incomplete_billing_details_require_nonempty_profile_id(self):
        for data in (
            {},
            {"paycode": "123"},
            {"flat": "46"},
            {"paycode": None, "flat": "  "},
        ):
            with self.subTest(data=data):
                self.assertIsNone(identity.place_unique_id(data))
                self.assertEqual(
                    identity.place_unique_id(data | {"user_place_id": 1234}),
                    "1234",
                )

    def test_empty_records_never_match(self):
        self.assertFalse(identity.places_match({}, {}))
        self.assertFalse(
            identity.places_match({"paycode": "123"}, {"paycode": "123"})
        )

    def test_profile_alias_detects_old_entry_with_incomplete_data(self):
        new = {"paycode": "123", "flat": "46", "user_place_id": "profile"}
        self.assertTrue(identity.places_match(new, {"user_place_id": "profile"}))
        self.assertTrue(
            identity.places_match(new, {}, right_unique_id="profile")
        )

    def test_stable_id_alias_detects_logged_out_entry(self):
        data = {"paycode": "123", "flat": "46"}
        self.assertTrue(
            identity.places_match(
                data, {}, right_unique_id=identity.place_unique_id(data)
            )
        )

    def test_fallback_names_include_profile_id(self):
        first = identity.place_title({"paycode": "123", "user_place_id": "a"})
        second = identity.place_title({"paycode": "123", "user_place_id": "b"})
        self.assertNotEqual(first, second)
        self.assertIn("ID a", first)
        self.assertEqual(identity.place_title({}), identity.DEFAULT_ENTRY_TITLE)

    def test_recognizes_both_historical_generated_title_formats(self):
        data = {"flat": "218"}
        self.assertTrue(
            identity.is_generated_legacy_title("MOS.RU Water Meter", data)
        )
        self.assertTrue(
            identity.is_generated_legacy_title("MOS.RU Water Meter_218", data)
        )
        self.assertFalse(identity.is_generated_legacy_title("Моя квартира", data))


def load_migration():
    source = SOURCE.parent / "__init__.py"
    tree = ast.parse(source.read_text())
    migrate = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_migrate_entry"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            migrate,
        ],
        type_ignores=[],
    )
    namespace = {
        name: getattr(identity, name)
        for name in (
            "is_generated_legacy_title",
            "place_unique_id",
            "places_match",
            "place_title",
        )
    }
    namespace.update(DOMAIN="mosru_water", _LOGGER=Mock())
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["async_migrate_entry"]


def make_entry(**updates):
    entry = SimpleNamespace(
        entry_id="unchanged-entry",
        unique_id=None,
        version=1,
        minor_version=1,
        title=identity.DEFAULT_ENTRY_TITLE,
        data={
            "paycode": "001234",
            "flat": "46",
            "user_place_id": "old-profile",
            "session_cookies": {"synthetic": "unchanged"},
            "cold_entity": "sensor.cold",
            "hot_entity": "sensor.hot",
            "cold_counter_id": "cold",
            "hot_counter_id": "hot",
            "submit_day": 20,
            "submission_blocked_month": "2026-11",
        },
        options={"submit_day": 21},
    )
    for key, value in updates.items():
        setattr(entry, key, value)
    return entry


class EntryMigrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = make_entry()
        self.entries = [self.entry]
        self.migrate = load_migration()

        def update_entry(entry, **updates):
            for key, value in updates.items():
                setattr(entry, key, value)

        self.manager = SimpleNamespace(
            async_entries=Mock(side_effect=lambda *args, **kwargs: self.entries),
            async_update_entry=Mock(side_effect=update_entry),
        )
        self.hass = SimpleNamespace(config_entries=self.manager)

    async def test_migration_preserves_entities_settings_and_operation_guard(self):
        data, options = deepcopy(self.entry.data), deepcopy(self.entry.options)
        old_entity_id = f"{self.entry.entry_id}_submit_button"
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.unique_id, 'flat:["001234","46"]')
        self.assertIn("ЕПД 001234", self.entry.title)
        self.assertIn("кв. 46", self.entry.title)
        self.assertEqual(self.entry.entry_id, "unchanged-entry")
        self.assertEqual(f"{self.entry.entry_id}_submit_button", old_entity_id)
        self.assertEqual(self.entry.data, data)
        self.assertEqual(self.entry.options, options)
        self.assertEqual((self.entry.version, self.entry.minor_version), (1, 2))
        self.assertEqual(
            set(self.manager.async_update_entry.call_args.kwargs),
            {"minor_version", "unique_id", "title"},
        )

    async def test_migrates_previous_profile_unique_id(self):
        self.entry.unique_id = "old-profile"
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.unique_id, 'flat:["001234","46"]')

    async def test_migrates_current_main_unique_id_format(self):
        self.entry.unique_id = "paycode:001234:flat:46"
        self.entry.title = "MOS.RU Water Meter · ЕПД 001234 · кв. 46"
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.unique_id, 'flat:["001234","46"]')
        self.assertEqual(
            self.entry.title, "MOS.RU Water Meter · ЕПД 001234 · кв. 46"
        )

    async def test_migrates_logged_out_legacy_without_network(self):
        self.entry.data.pop("user_place_id")
        self.entry.data["session_cookies"] = {}
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.unique_id, 'flat:["001234","46"]')

    async def test_migration_is_idempotent(self):
        self.assertTrue(await self.migrate(self.hass, self.entry))
        snapshot = deepcopy(vars(self.entry))
        self.manager.async_update_entry.reset_mock()
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(vars(self.entry), snapshot)
        self.manager.async_update_entry.assert_not_called()

    async def test_preserves_custom_title(self):
        self.entry.title = "Моя квартира"
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.title, "Моя квартира")
        self.assertNotIn("title", self.manager.async_update_entry.call_args.kwargs)

    async def test_migrates_underscore_generated_title(self):
        self.entry.title = "MOS.RU Water Meter_46"
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertIn("ЕПД 001234", self.entry.title)
        self.assertIn("кв. 46", self.entry.title)

    async def test_incomplete_record_uses_profile_fallback(self):
        self.entry.data.pop("flat")
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.unique_id, "old-profile")
        self.assertIn("ID old-profile", self.entry.title)

    async def test_record_without_identity_keeps_existing_id(self):
        self.entry.data = {"session_cookies": {}}
        self.entry.unique_id = "preserved-old-id"
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertEqual(self.entry.unique_id, "preserved-old-id")

    async def test_empty_records_are_not_false_duplicates(self):
        self.entry.data = {}
        self.entries.append(make_entry(entry_id="other", data={}))
        self.assertTrue(await self.migrate(self.hass, self.entry))
        self.assertIsNone(self.entry.unique_id)

    async def test_duplicate_legacy_entries_are_not_silently_merged(self):
        self.entries.append(
            make_entry(
                entry_id="duplicate",
                data={
                    "paycode": "001234",
                    "flat": 46,
                    "user_place_id": "different-account",
                },
            )
        )
        snapshot = deepcopy(vars(self.entry))
        self.assertFalse(await self.migrate(self.hass, self.entry))
        self.assertEqual(vars(self.entry), snapshot)
        self.manager.async_update_entry.assert_not_called()

    async def test_conflict_with_migrated_unique_id_is_rejected(self):
        self.entries.append(
            make_entry(
                entry_id="duplicate",
                data={},
                unique_id=identity.place_unique_id(self.entry.data),
                minor_version=2,
            )
        )
        self.assertFalse(await self.migrate(self.hass, self.entry))
        self.manager.async_update_entry.assert_not_called()

    async def test_another_flat_is_not_migration_conflict(self):
        self.entries.append(
            make_entry(
                entry_id="other",
                data={
                    "paycode": "001234",
                    "flat": "47",
                    "user_place_id": "other-profile",
                },
            )
        )
        self.assertTrue(await self.migrate(self.hass, self.entry))

    async def test_future_major_version_is_not_downgraded(self):
        self.entry.version = 2
        self.assertFalse(await self.migrate(self.hass, self.entry))
        self.manager.async_update_entry.assert_not_called()

    async def test_logout_cancel_add_regression_before_or_after_migration(self):
        for migrate_first in (False, True):
            with self.subTest(migrate_first=migrate_first):
                self.entry = make_entry()
                self.entries[:] = [self.entry]
                if migrate_first:
                    self.assertTrue(await self.migrate(self.hass, self.entry))
                self.manager.async_unload = AsyncMock(return_value=True)
                self.entry.async_start_reauth = Mock()
                logout = load_logout_methods()(self.entry)
                logout.hass = self.hass
                logout.async_abort = Mock()
                await logout.async_step_logout({})
                self.assertNotIn("user_place_id", self.entry.data)
                self.assertEqual(self.entry.data["session_cookies"], {})

                cls, _ = load_flow_methods()
                flow = cls()
                flow.async_set_unique_id = AsyncMock()
                flow._abort_if_unique_id_configured = Mock()
                flow._async_current_entries = Mock(return_value=self.entries)
                flow.async_step_discover = AsyncMock()
                flow.async_abort = Mock(return_value={"type": "abort"})
                await flow._async_select_place({
                    "paycode": "001234",
                    "flat": "46",
                    "user_place_id": "new-profile",
                })
                flow.async_abort.assert_called_once_with(
                    reason="already_configured"
                )
                flow.async_step_discover.assert_not_awaited()
                self.assertEqual(flow._data, {})

    async def test_logout_and_reauth_keep_migrated_id_and_settings(self):
        self.assertTrue(await self.migrate(self.hass, self.entry))
        before_id, before_options = self.entry.unique_id, deepcopy(self.entry.options)
        self.manager.async_unload = AsyncMock(return_value=True)
        self.entry.async_start_reauth = Mock()
        logout = load_logout_methods()(self.entry)
        logout.hass = self.hass
        logout.async_abort = Mock()
        await logout.async_step_logout({})
        self.assertEqual(self.entry.unique_id, before_id)

        cls, _ = load_flow_methods()
        flow = cls()
        flow._reauth_entry = self.entry
        flow._client = Mock()
        flow._client.get_session_cookies.return_value = {"synthetic": "new"}
        flow._qr_task = asyncio.get_running_loop().create_future()
        flow._qr_task.set_result(True)

        async def executor(fn, *args):
            return fn(*args)

        flow.hass = SimpleNamespace(async_add_executor_job=executor)

        def reload_and_abort(entry, data_updates):
            self.manager.async_update_entry(
                entry, data={**entry.data, **data_updates}
            )
            return {"type": "abort"}

        flow.async_update_reload_and_abort = Mock(side_effect=reload_and_abort)
        await flow.async_step_qr()
        self.assertEqual(self.entry.unique_id, before_id)
        self.assertEqual(self.entry.options, before_options)
        self.assertEqual(self.entry.entry_id, "unchanged-entry")
        self.assertEqual(self.entry.data["session_cookies"], {"synthetic": "new"})
        self.assertEqual(self.entry.data["cold_entity"], "sensor.cold")

    def test_flow_declares_matching_migration_version(self):
        tree = ast.parse(SOURCE.read_text())
        flow = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "MosRuWaterConfigFlow"
        )
        values = {
            target.id: node.value.value
            for node in flow.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id in {"VERSION", "MINOR_VERSION"}
        }
        self.assertEqual(values, {"VERSION": 1, "MINOR_VERSION": 2})
