"""Кнопка ручной отправки показаний mosru_water."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN
from .coordinator import MosRuWaterCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MosRuWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MosRuWaterSubmitButton(coordinator, entry)])


class MosRuWaterSubmitButton(ButtonEntity):
    """Кнопка 'Отправить показания сейчас'."""

    def __init__(self, coordinator: MosRuWaterCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_name = "MOS.RU Отправить показания"
        self._attr_unique_id = f"{entry.entry_id}_submit_button"

    async def async_press(self) -> None:
        try:
            result = await self._coordinator.async_submit_now()
            self._coordinator.async_set_updated_data(result)
        except ConfigEntryAuthFailed as err:
            _LOGGER.error("Требуется повторная авторизация: %s", err)
        except UpdateFailed as err:
            _LOGGER.error("Ошибка ручной отправки: %s", err)
