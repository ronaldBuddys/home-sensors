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


def bleak_kwargs(adapter: str | None) -> dict:
    """Keyword arguments that make a ``BleakScanner``/``BleakClient`` use ``adapter``."""
    return {"bluez": {"adapter": adapter}} if adapter else {}


def can_power_cycle() -> bool:
    """Power-cycling goes through BlueZ over D-Bus, so it is Linux only."""
    return sys.platform.startswith("linux")


async def power_cycle_adapter(adapter: str | None = None) -> str:
    """Power a BlueZ adapter off and on again over D-Bus (no root needed); return its name.

    Revives a controller that has stopped delivering advertising reports while BlueZ still
    says it is discovering (seen on the Raspberry Pi's onboard UART chip); restarting the
    scanner alone doesn't help there. ``adapter=None`` means the first adapter BlueZ lists.
    """
    from dbus_fast import BusType, Message, MessageType, Variant
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def call(path: str, interface: str, member: str, signature="", body=()):
        reply = await bus.call(
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

    try:
        if adapter:
            path = f"/org/bluez/{adapter}"
        else:
            (objects,) = await call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
            paths = sorted(p for p, ifaces in objects.items() if ADAPTER_IFACE in ifaces)
            if not paths:
                raise RuntimeError("no Bluetooth adapter found")
            path = paths[0]
        for powered in (False, True):
            await call(
                path,
                "org.freedesktop.DBus.Properties",
                "Set",
                "ssv",
                [ADAPTER_IFACE, "Powered", Variant("b", powered)],
            )
            await asyncio.sleep(1)
        return path.rsplit("/", 1)[-1]
    finally:
        bus.disconnect()
