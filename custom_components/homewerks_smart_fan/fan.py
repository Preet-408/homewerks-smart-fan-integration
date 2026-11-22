"""Fan platform for Homewerks Smart Fan."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HomewerksSmartFanApi
from .const import (
    ATTR_DURATION,
    DOMAIN,
    SCAN_INTERVAL as SCAN_INTERVAL_SECONDS,
    SERVICE_CANCEL_FAN_TIMER,
    SERVICE_RUN_FAN_FOR,
)
from .protocol import MAX_FAN_TIMER_SECONDS

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=SCAN_INTERVAL_SECONDS)


def _valid_duration(value: Any) -> timedelta:
    duration = cv.positive_time_period(value)
    if not timedelta(seconds=1) <= duration <= timedelta(seconds=MAX_FAN_TIMER_SECONDS):
        raise vol.Invalid("duration must be between 1 second and 23:59:59")
    return duration


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Homewerks Smart Fan from a config entry."""
    api: HomewerksSmartFanApi = hass.data[DOMAIN][entry.entry_id]
    name = entry.data.get(CONF_NAME, "Homewerks Smart Fan")

    async_add_entities([HomewerksSmartFanEntity(api, entry, name)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_RUN_FAN_FOR,
        {vol.Required(ATTR_DURATION): _valid_duration},
        "async_run_fan_for",
    )
    platform.async_register_entity_service(
        SERVICE_CANCEL_FAN_TIMER, {}, "async_cancel_fan_timer"
    )


class HomewerksSmartFanEntity(FanEntity):
    """Representation of a Homewerks Smart Fan."""

    _attr_has_entity_name = True
    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    _attr_icon = "mdi:fan"
    # This entity owns the periodic read-only GetStatus poll for the whole device.
    _attr_should_poll = True

    def __init__(
        self,
        api: HomewerksSmartFanApi,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        """Initialize the fan."""
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_fan"
        self._attr_name = "Fan"
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
        """Handle state update from the API."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return true if fan is on."""
        return self._api.state.get("fan_power", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the run timer."""
        ends = self._api.state.get("fan_timer_ends")
        return {"timer_ends": ends.isoformat() if ends else None}

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        if not await self._api.set_fan_power(True):
            raise HomeAssistantError("Could not reach the fan")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        if not await self._api.set_fan_power(False):
            raise HomeAssistantError("Could not reach the fan")

    async def async_run_fan_for(self, duration: timedelta) -> None:
        """Run the fan for a duration; the fan stops itself when the timer ends."""
        if not await self._api.set_fan_timer(int(duration.total_seconds())):
            raise HomeAssistantError("Could not reach the fan")

    async def async_cancel_fan_timer(self) -> None:
        """Cancel the run timer (the fan stops too, same as the app's Cancel)."""
        if not await self._api.cancel_fan_timer():
            raise HomeAssistantError("Could not reach the fan")

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._api.connected

    async def async_update(self) -> None:
        """Read-only status poll (GetStatus); replies arrive via the listener."""
        await self._api.request_state()
