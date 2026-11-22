"""Sensor platform for Homewerks Smart Fan: when the fan run timer ends."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HomewerksSmartFanApi
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fan timer sensor."""
    api: HomewerksSmartFanApi = hass.data[DOMAIN][entry.entry_id]
    name = entry.data.get(CONF_NAME, "Homewerks Smart Fan")
    async_add_entities([HomewerksFanTimerSensor(api, entry, name)])


class HomewerksFanTimerSensor(SensorEntity):
    """Timestamp when the running fan timer will stop the fan (unknown when no timer)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:fan-clock"
    _attr_should_poll = False

    def __init__(self, api: HomewerksSmartFanApi, entry: ConfigEntry, name: str) -> None:
        """Initialize the sensor."""
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_fan_timer_ends"
        self._attr_name = "Fan timer ends"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": name,
            "manufacturer": "Homewerks",
            "model": "7148-01-AX Smart Fan",
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        self._api.register_state_callback(self._handle_state_update)

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        self._api.unregister_state_callback(self._handle_state_update)

    @callback
    def _handle_state_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        """Return the end time, or None when no timer is running."""
        return self._api.state.get("fan_timer_ends")

    @property
    def available(self) -> bool:
        """Return True if the fan is connected."""
        return self._api.connected
