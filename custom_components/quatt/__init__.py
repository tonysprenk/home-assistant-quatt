"""Custom integration to integrate quatt with Home Assistant.

For more details about this integration, please refer to
https://github.com/marcoboers/home-assistant-quatt
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
import aiohttp

from .api import (
    QuattApiClient,
    QuattApiClientAuthenticationError,
    QuattApiClientCommunicationError,
    QuattApiClientError,
)
from .const import (
    CONF_POWER_SENSOR,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_CIC_ID,
    DOMAIN,
    LOGGER,
)
from .quatt_cloud import QuattCloud, QuattCloudConfig  # cloud client
from .coordinator import QuattDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    hass.data.setdefault(DOMAIN, {})

    # ---- Resolve a reachable host for the CIC local API ----
    ip_or_host = await _pick_reachable_host(hass, entry)

    if not ip_or_host:
        LOGGER.error(
            "Quatt local API host could not be resolved/reached. "
            "If mDNS/DNS is unavailable on your network, set a DHCP reservation + DNS A-record "
            "for your CIC hostname, or edit this config entry to include the CIC IP address."
        )
        return False

    coordinator = QuattDataUpdateCoordinator(
        hass=hass,
        update_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        client=QuattApiClient(
            ip_address=ip_or_host,
            session=async_get_clientsession(hass),
        ),
    )
    # Keep original structure so platforms can find the coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Coordinated initial refresh
    await coordinator.async_config_entry_first_refresh()

    # ---- Cloud client (non-invasive) ----
    try:
        data = entry.data
        if all(
            k in data
            for k in (
                "base_url",
                "firebase_api_key",
                "firebase_project_id",
                "firebase_project_number",
                "android_package",
                "android_cert",
                "firebase_client",
                "app_id",
                "app_instance_id",
                "app_device_id",
                "cic_serial",
            )
        ):
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

            # Best-effort discovery; non-fatal
            try:
                await cloud.async_discover()
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("Quatt cloud: discovery failed (non-fatal): %r", err)

            hass.data.setdefault(f"{DOMAIN}_cloud", {})
            hass.data[f"{DOMAIN}_cloud"][entry.entry_id] = cloud
        else:
            LOGGER.debug(
                "Quatt cloud: pairing fields not present in entry.data; skipping cloud client init"
            )
    except Exception as err:  # noqa: BLE001
        LOGGER.debug("Quatt cloud: failed to initialize cloud client (non-fatal): %r", err)

    # Forward the original platforms unchanged
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # On update of the options reload the entry which reloads the coordinator
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Cleanup cloud bucket if present
        cloud_bucket = hass.data.get(f"{DOMAIN}_cloud")
        if cloud_bucket is not None:
            cloud_bucket.pop(entry.entry_id, None)
            if not cloud_bucket:
                hass.data.pop(f"{DOMAIN}_cloud", None)

    return unloaded


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _get_cic_hostname(hass: HomeAssistant, ip_address: str) -> str:
    """Validate credentials."""
    client = QuattApiClient(
        ip_address=ip_address,
        session=async_create_clientsession(hass),
    )
    data = await client.async_get_data()
    return data["system"]["hostName"]


async def _migrate_v1_to_v2(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate v1 entry to v2 entry."""

    # Migrate CONF_POWER_SENSOR from data to options
    # Set the unique_id of the cic
    LOGGER.debug("Migrating config entry from version '%s'", config_entry.version)

    # The old version does not have a unique_id so we get the CIC hostname and set it
    # Return that the migration failed in case the retrieval fails
    try:
        hostname_unique_id = await _get_cic_hostname(
            hass=hass, ip_address=config_entry.data[CONF_IP_ADDRESS]
        )
    except QuattApiClientAuthenticationError as exception:
        LOGGER.warning(exception)
        return False
    except QuattApiClientCommunicationError as exception:
        LOGGER.error(exception)
        return False
    except QuattApiClientError as exception:
        LOGGER.exception(exception)
        return False
    else:
        # Validate that the hostname is found
        if (hostname_unique_id is not None) and (len(hostname_unique_id) >= 3):
            # Uppercase the first 3 characters CIC-xxxxxxxx-xxxx-xxxx-xxxxxxxxxxxx
            # This enables the correct match on DHCP hostname
            hostname_unique_id = hostname_unique_id[:3].upper() + hostname_unique_id[3:]

            new_data = {**config_entry.data}
            new_options = {**config_entry.options}

            if CONF_POWER_SENSOR in new_data:
                # Move the CONF_POWER_SENSOR to the options
                new_options[CONF_POWER_SENSOR] = new_data.pop(CONF_POWER_SENSOR)

            # Update the config entry to version 2
            hass.config_entries.async_update_entry(
                config_entry,
                data=new_data,
                options=new_options,
                unique_id=hostname_unique_id,
                version=2,
            )
        else:
            return False

    return True


async def _migrate_v2_to_v3(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate v2 entry to v3 entry."""

    # Remove the generic Heatpump device from the config entry data
    # Sensors are now created for the actual devices present in the system
    LOGGER.debug("Migrating config entry from version '%s'", config_entry.version)

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    # Clear the old Heatpump device from the device registry
    # This should only be one device, but we loop through all devices
    devices = dr.async_entries_for_config_entry(device_reg, config_entry.entry_id)
    for device in devices:
        for entity in er.async_entries_for_device(
            entity_reg, device.id, include_disabled_entities=True
        ):
            if entity.platform == DOMAIN:
                entity_reg.async_update_entity(entity.entity_id, device_id=None)

        # Remove the empty device
        device_reg.async_remove_device(device.id)

    # Update the config entry to version 3
    hass.config_entries.async_update_entry(
        config_entry,
        version=3,
    )

    return True


async def _migrate_v3_to_v4(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate v3 entry to v4 entry."""

    # Migration to hub/child layout + new unique_id format.
    # Old entity.unique_id:   entry.entry_id + sensor_key
    # New entity.unique_id:   f"{hub_id}:{device_identifier}:{sensor_key}"
    # Hub device:             (DOMAIN, hub_id)
    # Child device:           (DOMAIN, f"{hub_id}:{device_identifier}") via hub

    # include hub_id in device identifiers and entity unique_ids."
    LOGGER.debug("Migrating config entry from version '%s'", config_entry.version)

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    hub_id = config_entry.unique_id

    # Get the information about the devices for this config entry
    device_info: list[tuple[str, str, bool]] = []
    for device in dr.async_entries_for_config_entry(device_reg, config_entry.entry_id):
        # Check if this is the hub device or a child device
        if (DOMAIN, DEVICE_CIC_ID) in device.identifiers or (
            DOMAIN,
            hub_id,
        ) in device.identifiers:
            device_info.append((device.id, DEVICE_CIC_ID, True))
        else:
            device_identifier = next(iter(device.identifiers))[1]
            device_info.append((device.id, device_identifier, False))

    # Ensure hub comes first so via_device references a valid parent, sort on is_hub
    device_info.sort(key=lambda device_entry: 0 if device_entry[2] else 1)

    # Update devices and entities
    for device_id, device_identifier, is_hub in device_info:
        # Update the device identifiers and via_device_id (if not hub)
        device_reg.async_update_device(
            device_id,
            new_identifiers={
                (DOMAIN, hub_id if is_hub else f"{hub_id}:{device_identifier}")
            },
            via_device_id=None if is_hub else (DOMAIN, hub_id),
        )

        # Rewrite unique_ids for entities on this device: hub_id:<device_identifier>:<sensor_key>
        for entity in er.async_entries_for_device(
            entity_reg, device_id, include_disabled_entities=True
        ):
            # Checks are needed to avoid changing entities that are not part of this integration
            # or that have already been migrated.
            if (
                entity.config_entry_id != config_entry.entry_id
                or entity.platform != DOMAIN
            ):
                continue
            if entity.unique_id.startswith(f"{hub_id}:"):
                continue
            if not entity.unique_id.startswith(config_entry.entry_id):
                continue

            sensor_key = entity.unique_id[len(config_entry.entry_id) :]
            entity_reg.async_update_entity(
                entity.entity_id,
                new_unique_id=f"{hub_id}:{device_identifier}:{sensor_key}",
            )

    # Update the config entry to version 4
    hass.config_entries.async_update_entry(
        config_entry,
        version=4,
    )

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""

    if config_entry.version == 1:
        if not await _migrate_v1_to_v2(hass, config_entry):
            return False

    if config_entry.version == 2:
        if not await _migrate_v2_to_v3(hass, config_entry):
            return False

    if config_entry.version == 3:
        if not await _migrate_v3_to_v4(hass, config_entry):
            return False

    return True


# --------------------- Helpers ---------------------


async def _pick_reachable_host(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return a reachable host/ip for the CIC local API, or None."""
    # 1) Use explicit ip_address if present
    explicit = entry.data.get(CONF_IP_ADDRESS)
    cic_serial = (entry.data.get("cic_serial") or "").strip()

    # Original devices advertise a hostname like CIC-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)

    if cic_serial:
        # Ensure first 3 chars uppercase (CIC-...), just in case
        if len(cic_serial) >= 3:
            cic_serial = cic_serial[:3].upper() + cic_serial[3:]
        candidates.extend(
            [
                cic_serial,  # CIC-...
                cic_serial.lower(),  # cic-...
                f"{cic_serial}.local",
                f"{cic_serial.lower()}.local",
            ]
        )

    # De-dup while preserving order
    seen: set[str] = set()
    deduped = [c for c in candidates if not (c in seen or seen.add(c))]

    if not deduped:
        return None

    session = async_get_clientsession(hass)
    for host in deduped:
        if await _probe_host(session, host):
            if host != explicit:
                LOGGER.debug("Resolved CIC reachable host as '%s'", host)
            return host

    return None


async def _probe_host(session: aiohttp.ClientSession, host: str) -> bool:
    """Quickly check if we can GET the local feed on this host."""
    url = f"http://{host}:8080/beta/feed/data.json"
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with session.get(url, timeout=timeout) as resp:
            # 200 OK is ideal; some setups may redirect or return 401 > treat 200 only
            return resp.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False