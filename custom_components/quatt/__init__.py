from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .quatt_cloud import QuattCloud, QuattCloudConfig

_LOGGER = logging.getLogger(__name__)

# Keep the original platform list (from the repo). Example:
PLATFORMS = ["sensor", "climate"]  # do not change unless you add new ones


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    # Original setup remains (local CIC API, coordinator, etc.)
    # ... your existing code that creates 'api' / 'coordinator' stays here ...

    # NEW: create the cloud client from entry.data (populated by config_flow)
    data = entry.data
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

    # Try to discover installation id (non-fatal)
    try:
        await cloud.async_discover()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Cloud discovery failed (will retry later in commands): %r", err)

    hass.data.setdefault(DOMAIN, {})
    # Ensure we keep whatever the original integration put here:
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id]["cloud"] = cloud  # stash for future controls

    # Continue with the original platform forwarding:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok