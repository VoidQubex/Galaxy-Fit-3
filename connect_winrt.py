"""
Galaxy Fit 3 - connect and dump the GATT profile (diagnostic).

Connects straight to the bonded band by address via
BluetoothLEDevice.FromBluetoothAddressAsync, so it works even though the band
stops advertising once bonded (which is why scan.py won't find it). Requesting
the GATT services is what forces Windows to open the link.

Use this to sanity-check the link and see the band's services/characteristics.

Usage:
    python connect_winrt.py                    # default address below
    python connect_winrt.py 74:19:0A:07:9B:39
"""

import asyncio
import sys

from winrt.windows.devices.bluetooth import (
    BluetoothLEDevice,
    BluetoothConnectionStatus,
    BluetoothCacheMode,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCommunicationStatus,
    GattCharacteristicProperties,
)
from winrt.windows.storage.streams import DataReader

DEFAULT_ADDRESS = "74:19:0A:07:9B:39"

KNOWN = {
    "00001800-0000-1000-8000-00805f9b34fb": "Generic Access",
    "00001801-0000-1000-8000-00805f9b34fb": "Generic Attribute",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000180d-0000-1000-8000-00805f9b34fb": "Heart Rate",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery Service",
    "00001a1a-0000-1000-8000-00805f9b34fb": "Samsung SAP",
    "797ae4e9-2e58-4fe8-b48d-b5c79599fb9b": "SAP notify (band -> phone)",
    "63e30bad-4206-4596-839f-e47cbf7a4b5d": "SAP write  (phone -> band)",
    "00002a00-0000-1000-8000-00805f9b34fb": "Device Name",
    "00002a19-0000-1000-8000-00805f9b34fb": "Battery Level",
    "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
    "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
}

READABLE_TEXT = {
    "00002a00-0000-1000-8000-00805f9b34fb",
    "00002a24-0000-1000-8000-00805f9b34fb",
    "00002a26-0000-1000-8000-00805f9b34fb",
    "00002a29-0000-1000-8000-00805f9b34fb",
}
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"


def addr_to_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


def label(uuid) -> str:
    return KNOWN.get(str(uuid).lower(), "")


def prop_names(props) -> str:
    names = []
    for name, flag in (
        ("read", GattCharacteristicProperties.READ),
        ("write", GattCharacteristicProperties.WRITE),
        ("write-no-resp", GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE),
        ("notify", GattCharacteristicProperties.NOTIFY),
        ("indicate", GattCharacteristicProperties.INDICATE),
    ):
        if int(props) & int(flag):
            names.append(name)
    return ",".join(names) if names else "-"


def read_buffer(buf) -> bytes:
    reader = DataReader.from_buffer(buf)
    out = bytearray(buf.length)
    reader.read_bytes(out)
    return bytes(out)


async def main(mac: str) -> None:
    addr_int = addr_to_int(mac)
    print(f"Connecting to Galaxy Fit 3 at {mac} (0x{addr_int:012X}) ...")

    dev = await BluetoothLEDevice.from_bluetooth_address_async(addr_int)
    if dev is None:
        print("FAILED: Windows returned no device for that address.")
        print("The band must be bonded to this PC (Settings > Bluetooth).")
        sys.exit(1)
    print(f"Device: {dev.name!r}")

    # UNCACHED: Windows will otherwise hand back a stale, characteristic-less
    # view of the GATT DB right after connecting.
    svc_result = None
    for _ in range(6):
        svc_result = await dev.get_gatt_services_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED)
        if svc_result.status == GattCommunicationStatus.SUCCESS and list(svc_result.services):
            break
        await asyncio.sleep(1.0)

    if svc_result is None or svc_result.status != GattCommunicationStatus.SUCCESS:
        print(f"FAILED to read services: {svc_result.status if svc_result else '?'!r}")
        print("Usually means the band is connected to your phone, or asleep.")
        sys.exit(1)

    connected = dev.connection_status == BluetoothConnectionStatus.CONNECTED
    print(f"CONNECTED: {connected}\n")
    print("GATT services and characteristics")
    print("=" * 72)
    for service in svc_result.services:
        print(f"[Service] {service.uuid}  {label(service.uuid)}")
        ch_result = await service.get_characteristics_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED)
        if ch_result.status != GattCommunicationStatus.SUCCESS:
            print(f"    (could not read characteristics: {ch_result.status!r})")
            continue
        for ch in ch_result.characteristics:
            print(f"    - {ch.uuid}  ({prop_names(ch.characteristic_properties)})  "
                  f"{label(ch.uuid)}")
            u = str(ch.uuid).lower()
            if int(ch.characteristic_properties) & int(GattCharacteristicProperties.READ) \
                    and (u in READABLE_TEXT or u == BATTERY):
                try:
                    rr = await ch.read_value_async()
                    if rr.status == GattCommunicationStatus.SUCCESS:
                        data = read_buffer(rr.value)
                        if u == BATTERY:
                            print(f"        value: {data[0]}%")
                        else:
                            print(f"        value: {data.decode(errors='replace')!r}")
                except Exception as e:  # noqa: BLE001 - informational only
                    print(f"        (read failed: {e})")

    print("\n" + "=" * 72)
    print("SUCCESS - the PC is paired with and connected to the band.")
    print("Note: the band has NO standard Battery Service - battery comes over")
    print("SAP (service 00001a1a). Use server.py / battery.py for real data.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ADDRESS))
