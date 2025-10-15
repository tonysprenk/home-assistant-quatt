from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger("custom_components.quatt.config_flow")

STEP_USER_SCHEMA = vol.Schema({vol.Required("cic_serial"): str})
STEP_HOST_SCHEMA = vol.Schema({vol.Required(CONF_IP_ADDRESS): str})

# Mobile-app identifiers (public)
BASE_URL = "https://mobile-api.quatt.io/api/v1"
FIREBASE_API_KEY = "AIzaSyDM4PIXYDS9x53WUj-tDjOVAb6xKgzxX9Y"
FIREBASE_PROJECT_ID = "quatt-production"
FIREBASE_PROJECT_NUMBER = "1074628551428"
ANDROID_PACKAGE = "io.quatt.mobile.android"
ANDROID_CERT = "1110A8F9B0DE16D417086A4BDBCF956070F0FD97"
FIREBASE_CLIENT = "H4sIAAAAAAAAAKtWykhNLCpJSk0sKVayio7VUSpLLSrOzM9TslIyUqoFAFyivEQfAAAA"
APP_ID = "1:1074628551428:android:20ddeaf85c3cfec3336651"
APP_INSTANCE_ID = "dwNCvvXLQrqvmUJlZajYzG"
APP_DEVICE_ID = "ha-quatt"

PAIR_TIMEOUT_S = 120
POLL_INTERVAL_S = 2


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Quatt config flow with CIC pairing and optional host entry."""
    VERSION = 1

    _user_data: dict | None = None
    _pair_task: Optional[asyncio.Task] = None
    _pair_error: Optional[str] = None
    _pre_id_token: Optional[str] = None
    _pre_refresh_token: Optional[str] = None
    _resolved_host: Optional[str] = None

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        if user_input is None:
            _LOGGER.debug("Step user: showing CIC serial form")
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                description_placeholders={
                    "hint": "Find the CIC serial in the Quatt app: Settings → Device → Controller (CIC). Example: CIC-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                },
            )

        cic_serial = (user_input.get("cic_serial") or "").strip()
        if not cic_serial:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_SCHEMA, errors={"cic_serial": "required"}
            )
        self._user_data = {"cic_serial": cic_serial}
        _LOGGER.debug("Step user: CIC serial received = %s", cic_serial)
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict | None = None) -> FlowResult:
        """Maintain progress UI; when background finishes, close with progress_done -> host/create."""
        if self._user_data is None:
            return await self.async_step_user(user_input=None)

        # If task already finished, move forward
        if self._pair_task is not None and self._pair_task.done():
            _LOGGER.debug("Step pair: task finished; returning progress_done -> host/create")
            return self.async_show_progress_done(next_step_id="host")

        # Start background job once
        if self._pair_task is None:
            _LOGGER.debug("Step pair: creating background pairing task")
            self._pair_task = self.hass.async_create_task(self._pairing_background())

        # Show progress screen that will auto-advance when the task completes
        return self.async_show_progress(
            step_id="pair",
            progress_action="waiting_for_cic",
            progress_task=self._pair_task,
            description_placeholders={
                "msg": "Press the physical button on the CIC within ~2 minutes to confirm pairing."
            },
        )

    async def _pairing_background(self) -> None:
        """signup -> PUT /me -> requestPair -> poll until confirmed; then try auto host resolve."""
        assert self._user_data is not None
        cic_serial = self._user_data["cic_serial"]
        session = async_get_clientsession(self.hass)

        try:
            _LOGGER.debug("[pair] starting anonymous signup")
            id_token, refresh_token = await self._signup_anonymous(session)
            self._pre_id_token = id_token
            self._pre_refresh_token = refresh_token
            _LOGGER.debug("[pair] signup ok: id_token len=%s", len(id_token))

            _LOGGER.debug("[pair] PUT /me (register display name)")
            await self._put_me(session, id_token)

            _LOGGER.debug("[pair] POST requestPair for CIC serial: %s", cic_serial)
            await self._post_request_pair(session, id_token, cic_serial)
            _LOGGER.debug("[pair] requestPair accepted, polling for confirmation...")

            ok = await self._poll_confirmed(session, id_token, timeout_s=PAIR_TIMEOUT_S, interval_s=POLL_INTERVAL_S)
            self._pair_error = None if ok else "pair_failed"
            _LOGGER.debug("[pair] poll finished: ok=%s", ok)

            # Attempt to resolve a reachable host for local API, so we can skip the host step if possible
            if ok:
                self._resolved_host = await self._pick_reachable_host(session, cic_serial)

        except (aiohttp.ClientError, asyncio.TimeoutError) as net_err:
            _LOGGER.debug("[pair] network error: %r", net_err)
            self._pair_error = "connection"
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("[pair] unexpected error: %s", err)
            self._pair_error = "unknown"

    async def async_step_host(self, user_input: dict | None = None) -> FlowResult:
        """If we couldn't auto-resolve a local host, ask user for IP/hostname and validate it."""
        if self._pair_error:
            # Pairing failed; go back to user step with error
            self._pair_task = None
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": self._pair_error},
            )

        # If we already have a working host, skip straight to create
        if self._resolved_host:
            _LOGGER.debug("Step host: auto-resolved host '%s' — skipping host form", self._resolved_host)
            return await self.async_step_create()

        # Otherwise, ask user for an IP/hostname and validate
        if user_input is None:
            _LOGGER.debug("Step host: showing host form")
            return self.async_show_form(
                step_id="host",
                data_schema=STEP_HOST_SCHEMA,
                description_placeholders={
                    "hint": "Enter the CIC IP address or hostname. Example: 192.168.1.42 or CIC-xxxx....local"
                },
            )

        host = (user_input.get(CONF_IP_ADDRESS) or "").strip()
        if not host:
            return self.async_show_form(
                step_id="host", data_schema=STEP_HOST_SCHEMA, errors={"base": "required"}
            )

        session = async_get_clientsession(self.hass)
        if not await self._probe_host(session, host):
            return self.async_show_form(
                step_id="host", data_schema=STEP_HOST_SCHEMA, errors={"base": "cannot_connect"}
            )

        self._resolved_host = host
        return await self.async_step_create()

    async def async_step_create(self, user_input: dict | None = None) -> FlowResult:
        assert self._user_data is not None
        cic_serial = self._user_data["cic_serial"]

        if self._pair_error:
            self._pair_task = None
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors={"base": self._pair_error},
            )

        data = {
            "base_url": BASE_URL,
            "firebase_api_key": FIREBASE_API_KEY,
            "firebase_project_id": FIREBASE_PROJECT_ID,
            "firebase_project_number": FIREBASE_PROJECT_NUMBER,
            "android_package": ANDROID_PACKAGE,
            "android_cert": ANDROID_CERT,
            "firebase_client": FIREBASE_CLIENT,
            "app_id": APP_ID,
            "app_instance_id": APP_INSTANCE_ID,
            "app_device_id": APP_DEVICE_ID,
            "cic_serial": cic_serial,
            "pre_id_token": self._pre_id_token or "",
            "pre_refresh_token": self._pre_refresh_token or "",
        }
        # Include IP/host if we resolved or the user entered it
        if self._resolved_host:
            data[CONF_IP_ADDRESS] = self._resolved_host

        _LOGGER.debug("Step create: creating entry; cic=%s, host=%s", cic_serial, data.get(CONF_IP_ADDRESS))
        return self.async_create_entry(title="Quatt", data=data)

    # ---- HTTP helpers (config-flow only) ----
    async def _signup_anonymous(self, session) -> tuple[str, str]:
        url = f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/signupNewUser?key={FIREBASE_API_KEY}"
        headers = {
            "X-Android-Cert": ANDROID_CERT,
            "X-Android-Package": ANDROID_PACKAGE,
            "X-Client-Version": "Android/Fallback/X24000001/FirebaseCore-Android",
            "X-Firebase-GMPID": APP_ID,
            "X-Firebase-Client": FIREBASE_CLIENT,
            "content-type": "application/json",
            "User-Agent": "Quatt/HA",
        }
        async with session.post(url, headers=headers, json={"clientType": "CLIENT_TYPE_ANDROID"}, timeout=20) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message=body)
            js = await resp.json()
        id_token = js.get("idToken")
        refresh_token = js.get("refreshToken")
        if not id_token or not refresh_token:
            raise RuntimeError("signupNewUser returned no tokens")
        return id_token, refresh_token

    async def _put_me(self, session, id_token: str) -> None:
        headers = {
            "Authorization": f"Bearer {id_token}",
            "User-Agent": "Quatt/HA",
            "Content-Type": "application/json",
            "X-Device-Id": APP_DEVICE_ID,
        }
        async with session.put(f"{BASE_URL}/me", headers=headers, json={"firstName": "Home", "lastName": "Assistant"}, timeout=20) as resp:
            if resp.status not in (200, 204):
                _LOGGER.debug("PUT /me returned %s: %s", resp.status, await resp.text())

    async def _post_request_pair(self, session, id_token: str, cic_serial: str) -> None:
        headers = {
            "Authorization": f"Bearer {id_token}",
            "User-Agent": "Quatt/HA",
            "Content-Type": "application/json",
            "X-Device-Id": APP_DEVICE_ID,
        }
        url = f"{BASE_URL}/me/cic/{cic_serial}/requestPair"
        async with session.post(url, headers=headers, json={}, timeout=20) as resp:
            body = await resp.text()
            if resp.status not in (200, 204):
                raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message=body)

    async def _poll_confirmed(self, session, id_token: str, *, timeout_s: int, interval_s: int) -> bool:
        headers = {
            "Authorization": f"Bearer {id_token}",
            "User-Agent": "Quatt/HA",
            "Content-Type": "application/json",
            "X-Device-Id": APP_DEVICE_ID,
        }
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with session.get(f"{BASE_URL}/me/installations", headers=headers, timeout=20) as r:
                    if r.status == 200:
                        js = await r.json()
                        items = []
                        if isinstance(js, dict):
                            items = js.get("result") or js.get("items") or js.get("installations") or []
                        elif isinstance(js, list):
                            items = js
                        if items:
                            return True
            except Exception:
                pass
            try:
                async with session.get(f"{BASE_URL}/me", headers=headers, timeout=20) as r2:
                    if r2.status == 200:
                        js2 = await r2.json()
                        if isinstance(js2, dict) and js2.get("cic"):
                            return True
            except Exception:
                pass
            await asyncio.sleep(interval_s)
        return False

    # ---- Local host discovery/validation helpers ----
    async def _pick_reachable_host(self, session: aiohttp.ClientSession, cic_serial: str) -> str | None:
        """Return a reachable host/ip for the CIC local API, or None."""
        s = cic_serial.strip()
        if len(s) >= 3:
            s = s[:3].upper() + s[3:]
        candidates = [s, s.lower(), f"{s}.local", f"{s.lower()}.local"]
        seen: set[str] = set()
        for host in [c for c in candidates if not (c in seen or seen.add(c))]:
            if await self._probe_host(session, host):
                _LOGGER.debug("[host] auto-resolved working host: %s", host)
                return host
        return None

    async def _probe_host(self, session: aiohttp.ClientSession, host: str) -> bool:
        """Quickly check if we can GET the local feed on this host."""
        url = f"http://{host}:8080/beta/feed/data.json"
        try:
            timeout = aiohttp.ClientTimeout(total=4)
            async with session.get(url, timeout=timeout) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False