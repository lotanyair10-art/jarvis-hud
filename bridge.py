"""Home bridge: lets Jarvis (on Google Apps Script) control the Control4 system in the house.

Runs on a computer inside the home network. It never opens a port: it asks Jarvis for pending commands
(long polling), runs them against the local Control4 controller, and posts the results back.

Settings live in home-bridge/.env (C4_USERNAME, C4_PASSWORD, JARVIS_URL, JARVIS_TOKEN, optional C4_CONTROLLER_IP).
The Control4 password stays in that file on this computer and is only sent to Control4's own login service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

try:
    import async_timeout  # noqa: F401  pyControl4 imports it; newer Pythons don't get it with aiohttp
except ImportError:
    import types
    sys.modules["async_timeout"] = types.ModuleType("async_timeout")

import pyControl4.account
import pyControl4.director
from pyControl4.account import C4Account
from pyControl4.blind import C4Blind
from pyControl4.climate import C4Climate
from pyControl4.director import C4Director
from pyControl4.light import C4Light

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("home-bridge")


class _SyncTimeoutShim:
    """pyControl4 1.5 uses `with async_timeout.timeout(n)`, which async_timeout 4+ (needed by aiohttp)
    no longer supports. Requests still time out through aiohttp's own client timeouts."""

    class _NoOp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    @classmethod
    def timeout(cls, *args, **kwargs):
        return cls._NoOp()


pyControl4.account.async_timeout = _SyncTimeoutShim
pyControl4.director.async_timeout = _SyncTimeoutShim

TOKEN_TTL = 6 * 3600          # refresh the controller token well before it expires
INVENTORY_EVERY = 15 * 60     # re-send the device list every 15 minutes


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in {HERE / '.env'}")
    return value


def discover_controller(timeout: float = 3.0) -> str | None:
    """Find the controller's IP with Control4's SDDP multicast search."""
    msg = b'SEARCH * SDDP/1.0\r\nHost: "239.255.255.250:1902"\r\n\r\n'
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, ("239.255.255.250", 1902))
        end = time.time() + timeout
        while time.time() < end:
            data, addr = sock.recvfrom(4096)
            if b"control4" in data.lower() and b"controller" in data.lower():
                return addr[0]
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()
    return None


class Bridge:
    def __init__(self) -> None:
        self.username = env("C4_USERNAME")
        self.password = env("C4_PASSWORD")
        self.jarvis_url = env("JARVIS_URL")
        self.jarvis_token = env("JARVIS_TOKEN")
        self.ip = os.getenv("C4_CONTROLLER_IP", "").strip()
        self.director: C4Director | None = None
        self.token_at = 0.0
        self.kinds: dict[int, str] = {}
        self.dimmable: set[int] = set()

    # ---------------------------------------------------------------- Control4

    async def connect(self) -> None:
        account = C4Account(self.username, self.password)
        await account.getAccountBearerToken()
        controller = await account.getAccountControllers()
        token = await account.getDirectorBearerToken(controller["controllerCommonName"])
        if not self.ip:
            self.ip = discover_controller() or ""
            if not self.ip:
                sys.exit("Couldn't find the Control4 controller on the network. Put its IP in C4_CONTROLLER_IP in .env")
        no_verify = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        self.director = C4Director(self.ip, token["token"], no_verify)
        self.token_at = time.time()
        log.info("Connected to Control4 controller %s (%s)", controller.get("name", ""), self.ip)

    async def inventory(self) -> list[dict]:
        """Lights, thermostats and blinds, with their rooms."""
        found: dict[int, dict] = {}
        for category, kind in (("lights", "light"), ("thermostats", "thermostat"), ("motorization", "blind")):
            try:
                items = json.loads(await self.director.getAllItemsByCategory(category))
            except Exception as e:  # a category the system doesn't have
                log.debug("category %s: %s", category, e)
                continue
            for it in items if isinstance(items, list) else []:
                if not isinstance(it, dict) or "id" not in it:
                    continue
                proxy = str(it.get("proxy", ""))
                if kind == "light" and "light" not in proxy:
                    continue  # keypads and buttons also live in this category
                found[int(it["id"])] = {
                    "id": int(it["id"]), "name": it.get("name", ""), "room": it.get("roomName", ""),
                    "kind": kind, "dimmable": bool((it.get("capabilities") or {}).get("dimmer")) if kind == "light" else None,
                }
        self.kinds = {d["id"]: d["kind"] for d in found.values()}
        self.dimmable = {d["id"] for d in found.values() if d["dimmable"]}
        return list(found.values())

    async def light_status(self, device: int) -> dict:
        """Dimmers report LIGHT_LEVEL (or Brightness Percent on newer lights); plain switches only LIGHT_STATE."""
        for variable in ("LIGHT_LEVEL", "Brightness Percent"):
            try:
                return {"level": int(float(await self.director.getItemVariableValue(device, variable)))}
            except Exception:
                continue
        on = await self.director.getItemVariableValue(device, "LIGHT_STATE")
        return {"on": str(on) not in ("0", "False", "false", "")}

    async def run_command(self, cmd: dict) -> dict:
        device, action, value = int(cmd["device_id"]), cmd["action"], cmd.get("value")
        kind = self.kinds.get(device)
        if kind == "light":
            light = C4Light(self.director, device)
            if action in ("on", "off", "level"):
                level = 100 if action == "on" else 0 if action == "off" else max(0, min(100, int(float(value))))
                if action == "level" and device in self.dimmable:
                    await light.rampToLevel(level, 400)
                else:
                    await light.setLevel(level)  # plain (KNX) switches ignore ramps but accept SET_LEVEL
                await asyncio.sleep(1)
                return await self.light_status(device)
            return await self.light_status(device)
        if kind == "thermostat":
            climate = C4Climate(self.director, device)
            if action == "set_temperature":
                t = float(value)
                mode = (await climate.getHVACMode() or "").lower()
                if "heat" in mode:
                    await climate.setHeatSetpointC(t)
                else:
                    await climate.setCoolSetpointC(t)
            elif action == "hvac_mode":
                await climate.setHvacMode(str(value))  # e.g. Off, Cool, Heat, Auto
            elif action == "off":
                await climate.setHvacMode("Off")
            async def safe(getter):
                try:
                    return await getter()
                except Exception:
                    return None  # not every thermostat reports every value
            return {
                "mode": await safe(climate.getHVACMode), "current_c": await safe(climate.getCurrentTemperatureC),
                "cool_c": await safe(climate.getCoolSetpointC), "heat_c": await safe(climate.getHeatSetpointC),
            }
        if kind == "blind":
            blind = C4Blind(self.director, device)
            if action in ("open", "on"):
                await blind.open()
            elif action in ("close", "off"):
                await blind.close()
            elif action == "stop":
                await blind.stop()
            elif action == "level":
                await blind.setLevelTarget(max(0, min(100, int(float(value)))))
            return {"level": await blind.getLevel()}
        raise ValueError(f"Unknown device {device}")

    # ---------------------------------------------------------------- Jarvis

    async def jarvis(self, session: aiohttp.ClientSession, api: str, **payload) -> dict:
        body = json.dumps({"api": api, "token": self.jarvis_token, **payload})
        async with session.post(self.jarvis_url, data=body, headers={"Content-Type": "text/plain"},
                                timeout=aiohttp.ClientTimeout(total=60)) as r:
            out = json.loads(await r.text())
        if not out.get("ok"):
            raise RuntimeError(out.get("error"))
        return out.get("data")

    async def run(self) -> None:
        await self.connect()
        last_inventory = 0.0
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    if time.time() - self.token_at > TOKEN_TTL:
                        await self.connect()
                    if time.time() - last_inventory > INVENTORY_EVERY:
                        devices = await self.inventory()
                        await self.jarvis(session, "home_inventory", devices=devices)
                        last_inventory = time.time()
                        log.info("Sent %d devices to Jarvis", len(devices))
                    commands = await self.jarvis(session, "home_poll") or []
                    for cmd in commands:
                        try:
                            result = {"ok": True, "result": await self.run_command(cmd)}
                        except Exception as e:
                            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                        log.info("Command %s → %s", cmd, result)
                        await self.jarvis(session, "home_result", id=cmd["id"], result=result)
                except Exception as e:
                    log.warning("Loop error: %s", e)
                    await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(Bridge().run())
