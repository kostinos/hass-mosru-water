"""Apartment identity independent of an ed.mos.ru login or HA entity IDs."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

DEFAULT_ENTRY_TITLE = "MOS.RU Water Meter"
LEGACY_PROFILE_PREFIX = "user-place:"


def _text(value: Any) -> str:
    """Normalize portal numeric/string fields while preserving leading zeroes."""
    return "" if value is None else str(value).strip()


def place_unique_id(data: Mapping[str, Any]) -> str | None:
    """Return stable billing identity or the legacy profile ID as fallback."""
    paycode, flat = _text(data.get("paycode")), _text(data.get("flat"))
    if paycode and flat:
        return "flat:" + json.dumps(
            [paycode, flat], ensure_ascii=False, separators=(",", ":")
        )
    return _text(data.get("user_place_id")) or None


def unique_id_aliases(unique_id: str | None) -> set[str]:
    """Return canonical aliases for IDs emitted by previous releases."""
    value = _text(unique_id)
    if not value:
        return set()
    aliases = {value}
    if value.startswith(LEGACY_PROFILE_PREFIX) and (
        profile_id := value.removeprefix(LEGACY_PROFILE_PREFIX)
    ):
        aliases.add(profile_id)
    return aliases


def migrated_unique_id(
    data: Mapping[str, Any], current_unique_id: str | None
) -> str | None:
    """Choose the current ID, normalizing the temporary prefixed fallback."""
    if unique_id := place_unique_id(data):
        return unique_id
    if current_unique_id and current_unique_id.startswith(LEGACY_PROFILE_PREFIX):
        return current_unique_id.removeprefix(LEGACY_PROFILE_PREFIX) or None
    return _text(current_unique_id) or None


def _place_keys(data: Mapping[str, Any], unique_id: str | None) -> set[str]:
    """Return stable and legacy aliases available for duplicate detection."""
    keys = {
        key
        for key in (
            place_unique_id(data),
            _text(data.get("user_place_id")),
        )
        if key
    }
    return keys | unique_id_aliases(unique_id)


def places_match(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    left_unique_id: str | None = None,
    right_unique_id: str | None = None,
) -> bool:
    """Match new, unmigrated, disabled and logged-out entries."""
    return bool(
        _place_keys(left, left_unique_id)
        & _place_keys(right, right_unique_id)
    )


def place_title(data: Mapping[str, Any]) -> str:
    """Give generated entry names enough detail to distinguish apartments."""
    paycode, flat = _text(data.get("paycode")), _text(data.get("flat"))
    details = []
    if paycode:
        details.append(f"ЕПД {paycode}")
    if flat:
        details.append(f"кв. {flat}")
    if not (paycode and flat) and (
        place_id := _text(data.get("user_place_id"))
    ):
        details.append(f"ID {place_id}")
    return (
        f"{DEFAULT_ENTRY_TITLE} ({', '.join(details)})"
        if details
        else DEFAULT_ENTRY_TITLE
    )


def is_generated_legacy_title(title: str, data: Mapping[str, Any]) -> bool:
    """Recognize historical automatic titles without touching user renames."""
    generated = {DEFAULT_ENTRY_TITLE}
    if flat := _text(data.get("flat")):
        generated.add(f"{DEFAULT_ENTRY_TITLE}_{flat}")
    return title in generated
