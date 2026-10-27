"""Bluetooth adapter selection and recovery.

``ARANET_BLE_ADAPTER`` picks the local adapter by its BlueZ name (``hci0``, ``hci1``, ...), e.g.
a USB dongle instead of a board's onboard chip. Unset means bleak's default adapter. Adapter
selection only applies on Linux/BlueZ; on macOS and Windows the OS picks the adapter.
"""

from __future__ import annotations

import asyncio
import sys

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
MONITOR_MANAGER_IFACE = "org.bluez.AdvertisementMonitorManager1"


def bleak_kwargs(adapter: str | None) -> dict:
    """Keyword arguments that make a ``BleakScanner``/``BleakClient`` use ``adapter``."""
    return {"bluez": {"adapter": adapter}} if adapter else {}


def can_power_cycle() -> bool:
    """Power-cycling goes through BlueZ over D-Bus, so it is Linux only."""
    return sys.platform.startswith("linux")


class _BlueZ:
    """Minimal BlueZ D-Bus client (``async with``) for adapter-level calls bleak doesn't offer."""

    async def __aenter__(self) -> _BlueZ:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self

    async def __aexit__(self, *exc) -> None:
        self.bus.disconnect()

    async def call(self, path: str, interface: str, member: str, signature="", body=()):
        from dbus_fast import Message, MessageType

        reply = await self.bus.call(
            Message(
                destination=BLUEZ,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=list(body),
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")
        return reply.body

    async def adapters(self) -> dict[str, dict]:
        """Adapter object path -> its interfaces (and their properties)."""
        (objects,) = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return {p: ifaces for p, ifaces in objects.items() if ADAPTER_IFACE in ifaces}

    async def adapter_path(self, adapter: str | None) -> str:
        if adapter:
            return f"/org/bluez/{adapter}"
        paths = sorted(await self.adapters())
        if not paths:
            raise RuntimeError("no Bluetooth adapter found")
        return paths[0]


async def passive_scan_supported(adapter: str | None = None) -> bool:
    """Whether BlueZ offers passive scanning (``AdvertisementMonitorManager1``) on ``adapter``.

    BlueZ only exposes it with experimental features on (``Experimental = true`` in
    ``/etc/bluetooth/main.conf``, or ``bluetoothd -E``). ``adapter=None`` means the first
    adapter BlueZ lists, as for bleak's default. Any D-Bus failure counts as "no".
    """
    try:
        async with _BlueZ() as bluez:
            path = await bluez.adapter_path(adapter)
            ifaces = (await bluez.adapters()).get(path, {})
    except Exception:
        return False
    monitor = ifaces.get(MONITOR_MANAGER_IFACE)
    if monitor is None:
        return False
    types = monitor.get("SupportedMonitorTypes")
    return "or_patterns" in (types.value if types is not None else ())


async def power_cycle_adapter(adapter: str | None = None) -> str:
    """Power a BlueZ adapter off and on again over D-Bus (no root needed); return its name.

    Revives a controller that has stopped delivering advertising reports while BlueZ still
    says it is discovering (seen on the Raspberry Pi's onboard UART chip); restarting the
    scanner alone doesn't help there. ``adapter=None`` means the first adapter BlueZ lists.
    """
    from dbus_fast import Variant

    async with _BlueZ() as bluez:
        path = await bluez.adapter_path(adapter)
        for powered in (False, True):
            await bluez.call(
                path,
                "org.freedesktop.DBus.Properties",
                "Set",
                "ssv",
                [ADAPTER_IFACE, "Powered", Variant("b", powered)],
            )
            await asyncio.sleep(1)
        return path.rsplit("/", 1)[-1]
