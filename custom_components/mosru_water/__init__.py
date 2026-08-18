"""Интеграция mosru_water для Home Assistant."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import MosRuWaterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "button"]

SERVICE_REPLACE_READINGS = "replace_readings"
ATTR_ENTRY_ID = "entry_id"

_REPLACE_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Настройка интеграции из config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = MosRuWaterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Зарегистрировать сервисы домена (однократно)."""
    if hass.services.has_service(DOMAIN, SERVICE_REPLACE_READINGS):
        return

    async def _handle_replace(call: ServiceCall) -> None:
        """Перезаписать показания: удалить последние и отправить текущие.

        Отдельный сервис, а не автоматика: удаляется последнее показание
        независимо от источника, поэтому решение принимает пользователь.
        """
        coordinators: dict[str, MosRuWaterCoordinator] = hass.data.get(DOMAIN, {})
        entry_id = call.data.get(ATTR_ENTRY_ID)
        if entry_id:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise ServiceValidationError(f"Запись {entry_id} не найдена")
            targets = [coordinator]
        elif len(coordinators) == 1:
            targets = list(coordinators.values())
        else:
            raise ServiceValidationError(
                "Настроено несколько записей mosru_water — укажите entry_id"
            )

        for coordinator in targets:
            result = await coordinator.async_replace_readings()
            merged = dict(coordinator.data or {})
            merged.update(result)
            coordinator.async_set_updated_data(merged)

    hass.services.async_register(
        DOMAIN, SERVICE_REPLACE_READINGS, _handle_replace, schema=_REPLACE_SCHEMA
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузка интеграции."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_REPLACE_READINGS)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Применить новые options без перезагрузки HA."""
    coordinator: MosRuWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_config(dict(entry.data))
