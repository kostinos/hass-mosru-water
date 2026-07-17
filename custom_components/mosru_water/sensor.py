"""Сенсоры mosru_water."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNIT_M3
from .coordinator import MosRuWaterCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MosRuWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        MosRuWaterValueSensor(coordinator, entry, "cold", "Холодная вода (последнее)", "last_cold"),
        MosRuWaterValueSensor(coordinator, entry, "hot",  "Горячая вода (последнее)",  "last_hot"),
        MosRuWaterStatusSensor(coordinator, entry),
        MosRuWaterDateSensor(coordinator, entry),
    ])


class MosRuWaterValueSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Сенсор: последнее переданное значение (м³)."""

    _attr_native_unit_of_measurement = UNIT_M3
    _attr_device_class = SensorDeviceClass.WATER

    def __init__(
        self,
        coordinator: MosRuWaterCoordinator,
        entry: ConfigEntry,
        kind: str,
        name: str,
        data_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._data_key = data_key
        self._attr_name = f"MOS.RU {name}"
        self._attr_unique_id = f"{entry.entry_id}_{kind}_last"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            return self.coordinator.data.get(self._data_key)
        return None


class MosRuWaterStatusSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Сенсор: статус последней отправки."""

    def __init__(self, coordinator: MosRuWaterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "MOS.RU Статус отправки"
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        if self.coordinator.data:
            return self.coordinator.data.get("last_status", "pending")
        return "pending"


class MosRuWaterDateSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Сенсор: время последней отправки."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MosRuWaterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "MOS.RU Последняя отправка"
        self._attr_unique_id = f"{entry.entry_id}_submitted_at"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data:
            return self.coordinator.data.get("last_submitted_at")
        return None
