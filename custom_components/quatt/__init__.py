from __future__ import annotations

import logging
from importlib.util import find_spec

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .quatt_cloud import QuattCloud, QuattCloudConfig

_LOGGER = logging.getLogger(__name__)

# Try these; we'll filter to only those that are present in your repo
PLATFORM_CANDIDATES = [
    "sensor",
    "climate",
    "button",
    "number",
    "select",
    "switch",
    "binary_sensor",
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Quatt from a config entry."""
    data = entry.data

    # Build the cloud client (used now for discovery, later for controls)
    cloud_cfg = QuattCloudConfig(
        base_url=data["base_url"],
        firebase_api_key=data["firebase_api_key"],
        firebase_project_id=data["firebase_project_id"],
        firebase_project_number=data["firebase_project_number"],
        android_package=data["android_package"],
        android_cert=data["android_cert"],
        firebase_client=data["firebase_client"],
        app_id=data["app_id"],
        app_instance_id=data["app_instance_id"],
        app_device_id=data["app_device_id"],
        cic_serial=data["cic_serial"],
        pre_id_token=data.get("pre_id_token", ""),
        pre_refresh_token=data.get("pre_refresh_token", ""),
    )
    cloud = QuattCloud(hass, cloud_cfg)

    # Best-effort: discover installation id now (non-fatal)
    try:
        await cloud.async_discover()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Cloud discovery failed (will retry later if needed): %r", err)

    # Stash references (keep any existing data the original integration uses)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id]["cloud"] = cloud

    # Auto-detect which platform files actually exist in this repo
    platforms = [
        p for p in PLATFORM_CANDIDATES
        if find_spec(f"custom_components.{DOMAIN}.{p}") is not None
    ]
    _LOGGER.debug("Forwarding platforms for setup: %s", platforms)

    if platforms:
        await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    # Mirror the platforms we loaded on setup
    platforms = [
        p for p in PLATFORM_CANDIDATES
        if find_spec(f"custom_components.{DOMAIN}.{p}") is not None
    ]
    _LOGGER.debug("Unloading platforms: %s", platforms)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok