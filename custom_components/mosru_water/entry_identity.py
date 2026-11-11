"""Stable config-entry identity and generated titles."""
from __future__ import annotations

from typing import Any, Mapping


def place_unique_id(place: Mapping[str, Any]) -> str:
    """Return a flat identity that is independent of the mos.ru profile."""
    paycode = str(place.get("paycode") or "").strip()
    flat = str(place.get("flat") or "").strip()
    if paycode:
        return f"paycode:{paycode}:flat:{flat}"
    # Some incomplete profiles do not expose an EPD payer code. userPlaceId is
    # the only reliable identifier available in that case.
    return f"user-place:{place['user_place_id']}"


def entry_title(data: Mapping[str, Any]) -> str:
    """Return a generated title that distinguishes multiple config entries."""
    paycode = str(data.get("paycode") or "").strip()
    flat = str(data.get("flat") or "").strip()
    if paycode and flat:
        return f"MOS.RU Water Meter · ЕПД {paycode} · кв. {flat}"
    if paycode:
        return f"MOS.RU Water Meter · ЕПД {paycode}"
    return f"MOS.RU Water Meter · {data.get('user_place_id', '')}"


def legacy_entry_updates(
    title: str,
    unique_id: str | None,
    data: Mapping[str, Any],
) -> dict[str, str]:
    """Return safe updates for entries created before multi-flat support."""
    updates: dict[str, str] = {}
    if unique_id is None and (data.get("paycode") or data.get("user_place_id")):
        updates["unique_id"] = place_unique_id(data)

    flat = str(data.get("flat") or "").strip()
    generated_legacy_titles = {"MOS.RU Water Meter"}
    if flat:
        generated_legacy_titles.add(f"MOS.RU Water Meter_{flat}")
    if title in generated_legacy_titles:
        updates["title"] = entry_title(data)
    return updates
