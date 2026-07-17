"""Интеграция mosru_water для Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_COLD_ENTITY, CONF_HOT_ENTITY, CONF_SUBMIT_DAY
from .coordinator import MosRuWaterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции из config entry."""
    hass.data.setdefault(DOMAIN, {})

    entry_data = dict(entry.data)
    if entry.options:
        entry_data.update({
            CONF_COLD_ENTITY: entry.options.get(CONF_COLD_ENTITY, entry_data.get(CONF_COLD_ENTITY)),
            CONF_HOT_ENTITY:  entry.options.get(CONF_HOT_ENTITY,  entry_data.get(CONF_HOT_ENTITY)),
            CONF_SUBMIT_DAY:  entry.options.get(CONF_SUBMIT_DAY,  entry_data.get(CONF_SUBMIT_DAY)),
        })

    coordinator = MosRuWaterCoordinator(hass, entry_data)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузка интеграции."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Применить новые options без перезагрузки HA."""
    coordinator: MosRuWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry_data = dict(entry.data)
    if entry.options:
        entry_data.update(entry.options)
    coordinator.update_config(entry_data)
    await coordinator.async_request_refresh()
