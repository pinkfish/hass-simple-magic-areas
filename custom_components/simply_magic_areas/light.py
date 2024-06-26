"""Light controls for magic areas."""

from datetime import datetime
import logging

from homeassistant.components.group.light import LightGroup
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    DOMAIN as LIGHT_DOMAIN,
    LightEntityDescription,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_er
from homeassistant.helpers.event import async_track_state_change_event, call_later

from .base.entities import MagicEntity
from .base.magic import ControlType, MagicArea, StateConfigData
from .config.area_state import AreaState
from .config.entity_names import EntityNames
from .const import (
    ATTR_LAST_UPDATE_FROM_ENTITY,
    CONF_MANUAL_TIMEOUT,
    CONF_MAX_BRIGHTNESS_LEVEL,
    CONF_MIN_BRIGHTNESS_LEVEL,
    DATA_AREA_OBJECT,
    DEFAULT_MANUAL_TIMEOUT,
    DEFAULT_MAX_BRIGHTNESS_LEVEL,
    DEFAULT_MIN_BRIGHTNESS_LEVEL,
    DOMAIN,
    MODULE_DATA,
)

_LOGGER = logging.getLogger(__name__)
ATTR_LAST_ON_ILLUMINANCE: str = "last_on_illuminance"
ATTR_MANUAL_CONTROL: str = "manual_control"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the Area config entry."""
    area: MagicArea = hass.data[MODULE_DATA][config_entry.entry_id][DATA_AREA_OBJECT]
    existing_light_entities: list[str] = []
    if DOMAIN + LIGHT_DOMAIN in area.entities:
        existing_light_entities = [
            e[ATTR_ENTITY_ID] for e in area.entities[DOMAIN + LIGHT_DOMAIN]
        ]
    # Check if there are any lights
    if not area.has_entities(LIGHT_DOMAIN):
        _LOGGER.debug("No %s entities for area %s ", LIGHT_DOMAIN, area.name)
        _cleanup_light_entities(area.hass, [], existing_light_entities)
        return
    if not area.is_control_enabled(ControlType.Light):
        _LOGGER.info("%s: Lights disabled for area (%s) ", area.name, LIGHT_DOMAIN)
        _cleanup_light_entities(area.hass, [], existing_light_entities)
        return

    light_groups: list[AreaLightGroup] = []

    # Create light groups
    light_entities = [e[ATTR_ENTITY_ID] for e in area.entities[LIGHT_DOMAIN]]
    if area.is_meta():
        # light_groups.append(MagicLightGroup(area, light_entities))
        pass
    else:
        # Create the ones with no entity automatically plus ones with an entity set
        light_group_object = AreaLightGroup(area, light_entities)
        light_groups.append(light_group_object)

    # Create all groups
    async_add_entities(light_groups)
    group_ids: list[str] = [e.entity_id for e in light_groups]
    _cleanup_light_entities(area.hass, group_ids, existing_light_entities)


def _cleanup_light_entities(
    hass: HomeAssistant, new_ids: list[str], old_ids: list[str]
) -> None:
    entity_registry = async_get_er(hass)
    for ent_id in old_ids:
        if ent_id in new_ids:
            continue
        _LOGGER.warning("Deleting old entity %s", ent_id)
        entity_registry.async_remove(ent_id)


class AreaLightGroup(MagicEntity, LightGroup):
    """The light group to control the area lights specifically.

    There is one light group created that will mutate with the different
    sets of lights to control for the various states.  The state will
    always reflect the current state of the system and lights entities in
    that state.
    """

    def __init__(self, area: MagicArea, entities: list[str]) -> None:
        """Init the light group for the area."""
        MagicEntity.__init__(self, area, domain=LIGHT_DOMAIN, translation_key="light")
        LightGroup.__init__(
            self,
            name=None,
            entity_ids=entities,
            unique_id=self._attr_unique_id,
            mode=False,
        )

        self.entity_description = LightEntityDescription(
            key="light",
            name=f"{self.area.name} Lights (Simply Magic Areas)",
            icon="mdi:ceiling-light",
            device_class=LIGHT_DOMAIN,
        )

        delattr(self, "_attr_name")
        self._manual_timeout_cb: CALLBACK_TYPE | None = None
        self._attr_icon: str = "mdi:ceiling-light"

        # Add static attributes
        self.last_update_from_entity: bool = False
        self._attr_extra_state_attributes["lights"] = self._entity_ids
        self._attr_extra_state_attributes[ATTR_LAST_UPDATE_FROM_ENTITY] = False

    async def async_added_to_hass(self) -> None:
        """Run when this is added into hass."""
        # Get last state
        last_state = await self.async_get_last_state()

        if last_state:
            _LOGGER.debug(
                "%s restored [state=%s]",
                self.name,
                last_state.state,
            )
            self._attr_is_on = last_state.state == STATE_ON

            if ATTR_LAST_UPDATE_FROM_ENTITY in last_state.attributes:
                self.last_update_from_entity = bool(
                    last_state.attributes[ATTR_LAST_UPDATE_FROM_ENTITY]
                )
                self._attr_extra_state_attributes[ATTR_LAST_UPDATE_FROM_ENTITY] = (
                    self.last_update_from_entity
                )
        else:
            self._attr_is_on = False

        self.schedule_update_ha_state()

        # Setup state change listeners
        await self._setup_listeners()

        await super().async_added_to_hass()

    async def _setup_listeners(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self.entity_id],
                self._update_group_state,
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [
                    self.area.simply_magic_entity_id(SENSOR_DOMAIN, EntityNames.STATE),
                    self.area.simply_magic_entity_id(
                        SWITCH_DOMAIN, EntityNames.LIGHT_CONTROL
                    ),
                ],
                self._area_state_change,
            )
        )

    ### State Change Handling
    def _area_state_change(self, event: Event[EventStateChangedData]) -> None:
        if event.event_type != "state_changed":
            return
        if event.data["old_state"] is None or event.data["new_state"] is None:
            return

        from_state = event.data["old_state"].state
        if event.data["new_state"].state not in AreaState:
            _LOGGER.debug(
                "Light group (invalid to) %s. New state: %s / Last state %s",
                self.name,
                event.data["new_state"].state,
                from_state,
            )
            return
        to_state = AreaState(event.data["new_state"].state)

        _LOGGER.debug(
            "Light group %s. New state: %s / Last state %s",
            self.name,
            to_state,
            from_state,
        )

        if to_state is not None and self.area.has_configured_state(to_state):
            conf = self.area.state_config(to_state)
            if conf is not None:
                self._turn_on_light(conf)

    def _update_group_state(self, event: Event[EventStateChangedData]) -> None:
        if self.area.state != AreaState.AREA_STATE_CLEAR:
            self._reset_control()
        else:
            old_state = event.data["old_state"]
            new_state = event.data["new_state"]
            if old_state is None or new_state is None:
                return
            # Skip non ON/OFF state changes
            if old_state.state not in [
                STATE_ON,
                STATE_OFF,
            ]:
                return
            if new_state.state not in [
                STATE_ON,
                STATE_OFF,
            ]:
                return
            manual_timeout = self.area.config.get(
                CONF_MANUAL_TIMEOUT, DEFAULT_MANUAL_TIMEOUT
            )
            if old_state.attributes.get("restored"):
                # On state restored, also setup the timeout callback.
                if not self._is_controlled_by_this_entity():
                    if self._manual_timeout_cb is not None:
                        self._manual_timeout_cb()
                    self._manual_timeout_cb = call_later(
                        self.hass, manual_timeout, self._reset_manual_timeout
                    )
                return
            if self.last_update_from_entity:
                self.last_update_from_entity = False
                return
            self._set_controlled_by_this_entity(False)
            if self._manual_timeout_cb is not None:
                self._manual_timeout_cb()
            self._manual_timeout_cb = call_later(
                self.hass, manual_timeout, self._reset_manual_timeout
            )

    def _reset_manual_timeout(self, now: datetime):
        self._set_controlled_by_this_entity(True)
        self._manual_timeout_cb = None

    ####  Light Handling
    def _turn_on_light(self, conf: StateConfigData) -> None:
        """Turn on the light group."""

        self._entity_ids = conf.lights
        self.async_update_group_state()
        _LOGGER.debug(
            "Update light group %s %s %s %s %s",
            self.is_on,
            self.brightness,
            self.area.entities.keys(),
            conf.lights,
            self.area.entities[LIGHT_DOMAIN],
        )

        brightness = int(conf.dim_level * 255 / 100)
        if self.is_on and self.brightness == brightness:
            _LOGGER.debug("%s: Already on at %s", self.name, brightness)
            return

        luminesence = self._get_illuminance()
        min_brightness = self.area.config.get(
            CONF_MIN_BRIGHTNESS_LEVEL, DEFAULT_MIN_BRIGHTNESS_LEVEL
        )
        _LOGGER.debug(
            "%s: Checking brightness to %s %s,  %s, %s -- %s",
            self.name,
            self.is_on,
            luminesence,
            min_brightness,
            self.area.config.get(
                CONF_MAX_BRIGHTNESS_LEVEL, DEFAULT_MAX_BRIGHTNESS_LEVEL
            ),
            self._attr_extra_state_attributes.get(ATTR_LAST_ON_ILLUMINANCE, 0),
        )
        if luminesence > min_brightness:
            max_brightness = self.area.config.get(
                CONF_MAX_BRIGHTNESS_LEVEL, DEFAULT_MAX_BRIGHTNESS_LEVEL
            )
            if luminesence > max_brightness:
                brightness = 0
            else:
                diff = luminesence - min_brightness
                brightness = int(
                    brightness * (1.0 - diff / (max_brightness - min_brightness))
                )
                _LOGGER.debug(
                    "%s: Updating brightness to %s 1.0 - %s / %s",
                    self.name,
                    brightness,
                    diff,
                    (max_brightness - min_brightness),
                )
        if not self.area.is_control_enabled(ControlType.System):
            return

        if brightness == 0:
            _LOGGER.debug("%s: Brightness is 0", self.name)
            self._turn_off_light()
            return

        _LOGGER.debug("Turning on lights")
        self.last_update_from_entity = True
        service_data = {
            ATTR_ENTITY_ID: self.entity_id,
            ATTR_BRIGHTNESS: brightness,
        }
        self.hass.services.call(LIGHT_DOMAIN, SERVICE_TURN_ON, service_data)

        return

    def _turn_off_light(self) -> None:
        """Turn off the light group."""
        if not self.is_on:
            _LOGGER.debug("%s: Light already off", self.name)
            return

        if not self.area.is_control_enabled(ControlType.System):
            return

        self.last_update_from_entity = True
        service_data = {ATTR_ENTITY_ID: self.entity_id}
        # await self.async_turn_off()
        self.hass.services.call(LIGHT_DOMAIN, SERVICE_TURN_OFF, service_data)

        return

    def _get_illuminance(self) -> float:
        if self.is_on:
            if ATTR_LAST_ON_ILLUMINANCE in self._attr_extra_state_attributes:
                return (
                    self._attr_extra_state_attributes[ATTR_LAST_ON_ILLUMINANCE] or 0.0
                )
            return 0.0
        entity_id = self.area.simply_magic_entity_id(
            SENSOR_DOMAIN, EntityNames.ILLUMINANCE
        )

        sensor_entity = self.hass.states.get(entity_id)
        if sensor_entity is None:
            self._attr_extra_state_attributes[ATTR_LAST_ON_ILLUMINANCE] = 0.0
            return 0.0
        try:
            self._attr_extra_state_attributes[ATTR_LAST_ON_ILLUMINANCE] = float(
                sensor_entity.state
            )
            return float(sensor_entity.state)
        except ValueError:
            return 0.0

    #### Control Release
    def _is_controlled_by_this_entity(self) -> bool:
        return self._attr_extra_state_attributes.get(ATTR_MANUAL_CONTROL, False)

    def _set_controlled_by_this_entity(self, enabled: bool) -> None:
        self._attr_extra_state_attributes.get(ATTR_MANUAL_CONTROL, enabled)

    def _reset_control(self) -> None:
        self._set_controlled_by_this_entity(True)
        self.schedule_update_ha_state()
