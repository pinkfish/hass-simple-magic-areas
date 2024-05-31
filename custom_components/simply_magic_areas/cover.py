"""Cover controls for magic areas."""

import logging

from homeassistant.components.cover import (
    DOMAIN as COVER_DOMAIN,
    DEVICE_CLASSES as COVER_DEVICE_CLASSES,
)
from homeassistant.components.group.cover import CoverGroup
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base.entities import MagicEntity
from .base.magic import MagicArea
from .const import CONF_FEATURE_COVER_GROUPS, DATA_AREA_OBJECT, MODULE_DATA

_LOGGER = logging.getLogger(__name__)
DEPENDENCIES = ["magic_areas"]
ATTR_COVER_ENTITY_ID = "cover_entity_id"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up the Area config entry."""

    area: MagicArea = hass.data[MODULE_DATA][config_entry.entry_id][DATA_AREA_OBJECT]
    # Check feature availability
    if not area.has_feature(CONF_FEATURE_COVER_GROUPS):
        return

    # Check if there are any covers
    if not area.has_entities(COVER_DOMAIN):
        _LOGGER.debug("No %s entities for area %s", COVER_DOMAIN, area.name)
        return

    entities_to_add = []

    # Append None to the list of device classes to catch those covers that
    # don't have a device class assigned (and put them in their own group)
    for device_class in [*COVER_DEVICE_CLASSES, None]:
        covers_in_device_class = [
            e["entity_id"]
            for e in area.entities[COVER_DOMAIN]
            if e.get("device_class") == device_class
        ]

        if any(covers_in_device_class):
            _LOGGER.debug(
                "Creating %s cover group for %s with covers: %s",
                device_class,
                area.name,
                covers_in_device_class,
            )
            entities_to_add.append(AreaCoverGroup(area, device_class))
    async_add_entities(entities_to_add)


class AreaCoverGroup(MagicEntity, CoverGroup):
    """Cover group for handling all the covers in the area."""

    def __init__(self, area: MagicArea, device_class: str) -> None:
        """Initialize the cover group."""
        MagicEntity.__init__(self, area=area, translation_key=f"cover_{device_class}")

        self._device_class = device_class
        self._entities = [
            e
            for e in area.entities[COVER_DOMAIN]
            if e.get("device_class") == device_class
        ]
        self._attributes[ATTR_COVER_ENTITY_ID] = [
            e[ATTR_ENTITY_ID] for e in self._entities
        ]

        CoverGroup.__init__(
            self, self.unique_id, "", self._attributes[ATTR_COVER_ENTITY_ID]
        )
        delattr(self, "_attr_name")

    @property
    def device_class(self):
        """The cover device classes."""
        return self._device_class
