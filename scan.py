"""
Galaxy Fit 3 - BLE scanner (diagnostic).

Scans for nearby Bluetooth LE devices and highlights anything that looks like a
Galaxy Fit 3. Only useful when the band is ADVERTISING - i.e. in pairing mode or
not currently bonded/connected to anything. Once it's bonded to this PC it goes
quiet, and you should use connect_winrt.py / server.py (connect-by-address).

Usage:
    python scan.py            # scan for ~12 seconds
    python scan.py 20         # scan for 20 seconds
"""

import asyncio
import sys

from bleak import BleakScanner

FIT3_HINTS = ("fit3", "fit 3", "galaxy fit")
SAMSUNG_HINTS = ("galaxy", "samsung", "sm-r")


def classify(name: str) -> str:
    low = (name or "").lower()
    if any(h in low for h in FIT3_HINTS):
        return "FIT3"
    if any(h in low for h in SAMSUNG_HINTS):
        return "SAMSUNG?"
    return ""


async def main(timeout: float) -> None:
    print(f"Scanning for BLE devices for {timeout:.0f}s...\n")
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    if not discovered:
        print("No BLE devices found at all. Is Bluetooth on?")
        return

    rows = []
    for address, (device, adv) in discovered.items():
        name = adv.local_name or device.name or "(unknown)"
        rows.append((adv.rssi if adv.rssi is not None else -999, address, name, adv))
    rows.sort(reverse=True)

    fit3_hits = []
    print(f"{'RSSI':>5}  {'ADDRESS':<20} {'NAME':<28} TAG")
    print("-" * 70)
    for rssi, address, name, adv in rows:
        tag = classify(name)
        if tag == "FIT3":
            fit3_hits.append((address, name, adv))
        print(f"{rssi:>5}  {address:<20} {name[:28]:<28} {tag}")

    print("\n" + "=" * 70)
    if fit3_hits:
        print("Found what looks like a Galaxy Fit 3:\n")
        for address, name, adv in fit3_hits:
            print(f"  name     : {name}")
            print(f"  address  : {address}")
            svc = list(adv.service_uuids or [])
            print(f"  services : {svc if svc else '(none advertised)'}")
            md = adv.manufacturer_data or {}
            if md:
                print("  mfr data :")
                for cid, val in md.items():
                    tag = " (Samsung)" if cid == 0x0075 else ""
                    print(f"     0x{cid:04x}{tag}: {val.hex()}")
            print()
        print("Next: python connect_winrt.py <address>")
    else:
        print("No Galaxy Fit 3 detected.")
        print("A band that's already bonded to this PC does NOT advertise, so this")
        print("is expected once pairing is done - use connect_winrt.py instead.")
        print("If you're trying to pair fresh: put it in range, disconnect it from")
        print("your phone (a BLE peripheral holds one link), then run this again.")


if __name__ == "__main__":
    t = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    asyncio.run(main(t))
