"""Config entry identity and safe legacy-title migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components/mosru_water/entry_identity.py"
)
SPEC = importlib.util.spec_from_file_location("mosru_water_entry_identity", PATH)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


class EntryIdentityTest(unittest.TestCase):
    data = {
        "paycode": "1730249056",
        "flat": "218",
        "user_place_id": "1152619",
    }

    def test_legacy_generated_title_and_missing_unique_id_are_upgraded(self):
        self.assertEqual(
            identity.legacy_entry_updates(
                "MOS.RU Water Meter_218", None, self.data
            ),
            {
                "title": "MOS.RU Water Meter · ЕПД 1730249056 · кв. 218",
                "unique_id": "paycode:1730249056:flat:218",
            },
        )

    def test_custom_title_is_preserved(self):
        self.assertEqual(
            identity.legacy_entry_updates("Квартира родителей", None, self.data),
            {"unique_id": "paycode:1730249056:flat:218"},
        )

    def test_completed_entry_needs_no_updates(self):
        self.assertEqual(
            identity.legacy_entry_updates(
                "Квартира родителей",
                "paycode:1730249056:flat:218",
                self.data,
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
