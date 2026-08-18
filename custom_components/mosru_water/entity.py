"""Базовый класс и DeviceInfo для сущностей mosru_water."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CONF_PAYCODE


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    paycode = entry.data.get(CONF_PAYCODE, "")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Водосчётчик ЖКУ {paycode}",
        manufacturer="Портал mos.ru",
        model="Счётчики воды",
        entry_type=DeviceEntryType.SERVICE,
        # Ведёт туда, куда интеграция реально пишет показания
        configuration_url="https://ed.mos.ru/lk/counters/",
    )
