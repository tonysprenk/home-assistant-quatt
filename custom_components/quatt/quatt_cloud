from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger("custom_components.quatt.quatt_cloud")


@dataclass
class QuattCloudConfig:
    base_url: str
    firebase_api_key: str
    firebase_project_id: str
    firebase_project_number: str
    android_package: str
    android_cert: str
    firebase_client: str
    app_id: str
    app_instance_id: str
    app_device_id: str
    cic_serial: str
    pre_id_token: str = ""
    pre_refresh_token: str = ""


class QuattCloud:
    """Tiny cloud client (discovery now; controls can be added later)."""

    def __init__(self, hass: HomeAssistant, cfg: QuattCloudConfig) -> None:
        self.hass = hass
        self.cfg = cfg
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        self._id_token: Optional[str] = cfg.pre_id_token or None
        self._refresh_token: Optional[str] = cfg.pre_refresh_token or None
        self.installation_id: Optional[str] = None

    # ---------- public API ----------

    async def async_discover(self) -> str:
        """Populate installation_id from /me/installations."""
        await self._ensure_token()
        headers = self._headers()
        url = f"{self.cfg.base_url}/me/installations"
        async with self._session.get(url, headers=headers, timeout=20) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message=body)
            js = await resp.json()

        items: list[Any] = []
        if isinstance(js, dict):
            items = js.get("result") or js.get("items") or js.get("installations") or []
        elif isinstance(js, list):
            items = js

        if not items:
            raise RuntimeError("No installations found after pairing; press CIC button again?")

        # Prefer first active installation
        inst = next((x for x in items if isinstance(x, dict) and x.get("status") == "active"), items[0])
        self.installation_id = inst.get("externalId") or inst.get("id") or inst.get("installationId")
        _LOGGER.debug("Discovered installation id: %s", self.installation_id)
        return self.installation_id

    # Future: command execution will go here
    # async def async_execute_command(self, ...): ...

    # ---------- low-level helpers ----------

    async def _ensure_token(self) -> None:
        if self._id_token:
            return
        if not self._refresh_token:
            raise RuntimeError("No tokens available for cloud calls")
        await self._refresh_id_token()

    async def _refresh_id_token(self) -> None:
        url = f"https://securetoken.googleapis.com/v1/token?key={self.cfg.firebase_api_key}"
        headers = {
            "X-Android-Cert": self.cfg.android_cert,
            "X-Android-Package": self.cfg.android_package,
            "X-Client-Version": "Android/Fallback/X24000001/FirebaseCore-Android",
            "X-Firebase-GMPID": self.cfg.app_id,
            "X-Firebase-Client": self.cfg.firebase_client,
            "content-type": "application/json",
            "User-Agent": "Quatt/HA",
        }
        data = {"grantType": "refresh_token", "refreshToken": self._refresh_token}
        async with self._session.post(url, headers=headers, json=data, timeout=20) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message=body)
            js = await resp.json()
        self._id_token = js.get("id_token")
        self._refresh_token = js.get("refresh_token", self._refresh_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._id_token}",
            "User-Agent": "Quatt/HA",
            "Content-Type": "application/json",
            "X-Device-Id": self.cfg.app_device_id,
        }

    async def _q_get(self, path: str) -> Any:
        await self._ensure_token()
        url = f"{self.cfg.base_url}{path}"
        async with self._session.get(url, headers=self._headers(), timeout=20) as resp:
            if resp.status == 401 and self._refresh_token:
                await self._refresh_id_token()
                return await self._q_get(path)
            body = await resp.text()
            if resp.status != 200:
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message=body)
            return await resp.json()

    async def _q_post(self, path: str, payload: Any) -> Any:
        await self._ensure_token()
        url = f"{self.cfg.base_url}{path}"
        async with self._session.post(url, headers=self._headers(), json=payload, timeout=20) as resp:
            if resp.status == 401 and self._refresh_token:
                await self._refresh_id_token()
                return await self._q_post(path, payload)
            body = await resp.text()
            if resp.status not in (200, 204):
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message=body)
            return await (resp.json() if resp.content_length else asyncio.sleep(0))