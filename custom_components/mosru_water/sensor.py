"""Сенсоры mosru_water."""
from __future__ import annotations

from datetime import date

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNIT_M3
from .coordinator import MosRuWaterCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MosRuWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    dev = build_device_info(entry)
    eid = entry.entry_id
    async_add_entities([
        MosRuWaterValueSensor(coordinator, dev, eid, "cold_cur", "Холодная (mos.ru)",        "cold_current",  "mdi:water",             "cold_number"),
        MosRuWaterValueSensor(coordinator, dev, eid, "hot_cur",  "Горячая (mos.ru)",          "hot_current",   "mdi:water-thermometer", "hot_number"),
        MosRuWaterValueSensor(coordinator, dev, eid, "cold",     "Холодная (отправлено)",      "last_cold",     "mdi:water-check"),
        MosRuWaterValueSensor(coordinator, dev, eid, "hot",      "Горячая (отправлено)",       "last_hot",      "mdi:water-check"),
        MosRuWaterStatusSensor(coordinator, dev, eid),
        MosRuWaterDateSensor(coordinator, dev, eid),
        MosRuWaterInspectionSensor(coordinator, dev, eid, "cold", "Поверка холодного счётчика", "cold_inspection_date"),
        MosRuWaterInspectionSensor(coordinator, dev, eid, "hot",  "Поверка горячего счётчика",  "hot_inspection_date"),
        MosRuWaterInspectionStatusSensor(coordinator, dev, eid, "cold", "Статус поверки холодного", "cold_inspection_status"),
        MosRuWaterInspectionStatusSensor(coordinator, dev, eid, "hot",  "Статус поверки горячего",  "hot_inspection_status"),
        MosRuWaterReadingPeriodSensor(coordinator, dev, eid, "cold", "Период показания холодного", "cold_reading_period"),
        MosRuWaterReadingPeriodSensor(coordinator, dev, eid, "hot",  "Период показания горячего",  "hot_reading_period"),
        MosRuWaterSubmitAvailableSensor(coordinator, dev, eid),
    ])


class MosRuWaterValueSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Показания счётчика (м³)."""

    _attr_native_unit_of_measurement = UNIT_M3
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, device_info, entry_id, kind, name, data_key, icon, number_key=None) -> None:
        super().__init__(coordinator)
        self._data_key = data_key
        self._number_key = number_key
        self._attr_name = f"MOS.RU {name}"
        self._attr_unique_id = f"{entry_id}_{kind}"
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(self._data_key)

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self._number_key:
            return None
        number = (self.coordinator.data or {}).get(self._number_key)
        return {"serial_number": number} if number else None


class MosRuWaterStatusSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Статус последней отправки."""

    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator, device_info, entry_id) -> None:
        super().__init__(coordinator)
        self._attr_name = "MOS.RU Статус отправки"
        self._attr_unique_id = f"{entry_id}_status"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str:
        return (self.coordinator.data or {}).get("last_status", "pending")


class MosRuWaterDateSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Дата и время последней отправки."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator, device_info, entry_id) -> None:
        super().__init__(coordinator)
        self._attr_name = "MOS.RU Последняя отправка"
        self._attr_unique_id = f"{entry_id}_submitted_at"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("last_submitted_at")


class MosRuWaterInspectionSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Дата плановой поверки счётчика."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, device_info, entry_id, kind, name, data_key) -> None:
        super().__init__(coordinator)
        self._data_key = data_key
        self._attr_name = f"MOS.RU {name}"
        self._attr_unique_id = f"{entry_id}_{kind}_inspection"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> date | None:
        raw = (self.coordinator.data or {}).get(self._data_key)
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None


class MosRuWaterInspectionStatusSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Статус поверки счётчика (OK / EXPIRED / …)."""

    _attr_icon = "mdi:shield-check"

    def __init__(self, coordinator, device_info, entry_id, kind, name, data_key) -> None:
        super().__init__(coordinator)
        self._data_key = data_key
        self._attr_name = f"MOS.RU {name}"
        self._attr_unique_id = f"{entry_id}_{kind}_inspection_status"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get(self._data_key) or None


class MosRuWaterReadingPeriodSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Дата периода текущего показания на mos.ru."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-today"

    def __init__(self, coordinator, device_info, entry_id, kind, name, data_key) -> None:
        super().__init__(coordinator)
        self._data_key = data_key
        self._attr_name = f"MOS.RU {name}"
        self._attr_unique_id = f"{entry_id}_{kind}_reading_period"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> date | None:
        raw = (self.coordinator.data or {}).get(self._data_key)
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None


class MosRuWaterSubmitAvailableSensor(CoordinatorEntity[MosRuWaterCoordinator], SensorEntity):
    """Доступность отправки показаний прямо сейчас."""

    _attr_icon = "mdi:send-check"

    def __init__(self, coordinator, device_info, entry_id) -> None:
        super().__init__(coordinator)
        self._attr_name = "MOS.RU Отправка доступна"
        self._attr_unique_id = f"{entry_id}_submit_available"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        cold_ro = data.get("cold_readonly", True)
        hot_ro  = data.get("hot_readonly", True)
        if not cold_ro and not hot_ro:
            return "да"
        if cold_ro and hot_ro:
            return "нет"
        blocked = []
        if cold_ro:
            blocked.append("холодная")
        if hot_ro:
            blocked.append("горячая")
        return f"частично (заблокирована: {', '.join(blocked)})"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "cold_readonly": data.get("cold_readonly"),
            "hot_readonly":  data.get("hot_readonly"),
        }
