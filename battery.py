"""
Galaxy Fit 3 - persistent connection + battery level over Samsung SAP.

Maintains an always-on BLE link (GattSession.MaintainConnection), performs the
SAP HELLO handshake + OOBE init the band needs, then requests the battery level
and prints it. Keeps the connection open and re-polls unless --once is given.

Usage:
    python battery.py                 # connect, read battery, keep polling
    python battery.py --once          # read battery once, then exit
    python battery.py 74:19:0A:07:9B:39 --once

Ctrl+C to stop.
"""

import asyncio
import sys
from datetime import datetime

from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattSession,
    GattCommunicationStatus,
    GattClientCharacteristicConfigurationDescriptorValue as CCCD,
    GattWriteOption,
)
from winrt.windows.storage.streams import DataReader, DataWriter

import sap

DEFAULT_ADDRESS = "74:19:0A:07:9B:39"
SVC_1A1A = "00001a1a-0000-1000-8000-00805f9b34fb"
CH_NOTIFY = "797ae4e9-2e58-4fe8-b48d-b5c79599fb9b"   # band -> phone
CH_WRITE = "63e30bad-4206-4596-839f-e47cbf7a4b5d"    # phone -> band

# Announced to the band in the HELLO; any values work (see reference).
PHONE_MODEL = "M2007J3SY"
PHONE_VENDOR = "Xiaomi"
POLL_SECONDS = 60


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def addr_to_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


def buf_to_bytes(buf) -> bytes:
    reader = DataReader.from_buffer(buf)
    out = bytearray(buf.length)
    reader.read_bytes(out)
    return bytes(out)


def bytes_to_buf(data: bytes):
    w = DataWriter()
    w.write_bytes(bytes(data))
    return w.detach_buffer()


class Fit3Battery:
    def __init__(self, mac: str, once: bool):
        self.mac = mac
        self.once = once
        self.seq = 0x40
        self.write_char = None
        self.loop = asyncio.get_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.handshake_done = False
        self.got_battery = asyncio.Event()

    # ---- low-level writes ----
    async def _write(self, frame: bytes, reason: str):
        status = await self.write_char.write_value_async(bytes_to_buf(frame))
        ok = status == GattCommunicationStatus.SUCCESS
        shown = frame.hex(' ')
        if len(shown) > 60:
            shown = shown[:60] + '...'
        print(f"[{ts()}] TX {reason:<16} {shown} {'' if ok else '(WRITE FAILED)'}")

    def _next_seq(self) -> int:
        s = self.seq
        self.seq = (self.seq + 0x10) & 0xFF
        if self.seq == 0x00:
            self.seq = 0x40
        return s

    async def _send_message(self, channel: int, body: bytes, reason: str):
        frame = sap.frame_wrap(sap.packet_encode_single(channel, self._next_seq(), body))
        await self._write(frame, reason)

    async def _send_raw(self, frame: bytes, reason: str):
        await self._write(frame, reason)

    # ---- handshake + requests ----
    async def _on_band_hello(self, frame: bytes):
        bh = sap.parse_band_hello(frame)
        if bh is None:
            print(f"[{ts()}] band HELLO unparseable ({len(frame)} bytes)")
            return
        print(f"[{ts()}] band HELLO: model='{bh['model']}' vendor='{bh['vendor']}'")
        reply = sap.build_phone_hello(bh, sap.random_token(), PHONE_MODEL, PHONE_VENDOR, None)
        await self._send_raw(reply, "hello-reply")
        # OOBE steps the band needs before it services other channels, then battery.
        await self._send_message(sap.CH_CONTROL, sap.build_init_setting(), "init-setting")
        await self._send_message(sap.CH_CONTROL, sap.build_license_request(True), "license")
        await self.request_battery()
        self.handshake_done = True

    async def request_battery(self):
        await self._send_message(sap.CH_SETTINGS, sap.build_battery_request(), "battery-request")

    # ---- receive path ----
    async def _handle_frame(self, frame: bytes):
        if sap.frame_is_hello(frame):
            if sap.hello_is_band(frame):
                await self._on_band_hello(frame)
            return
        payload = sap.frame_unwrap(frame)
        if payload is None:
            print(f"[{ts()}] RX dropped (bad CRC/len): {frame.hex(' ')[:48]}")
            return
        pkt = sap.packet_parse(payload)
        kind = pkt["kind"]
        if kind == "ack":
            return
        if kind in ("single", "first"):
            # ACK it so the band keeps talking.
            await self._send_raw(sap.frame_wrap(sap.packet_encode_ack(pkt["seq"], kind == "first")),
                                 "ack")
            await self._deliver(pkt["channel"], pkt["body"])
        elif kind == "cont":
            await self._send_raw(sap.frame_wrap(sap.packet_encode_ack(pkt["seq"], True)), "ack")

    async def _deliver(self, channel: int, body: bytes):
        if channel == sap.CH_SETTINGS:
            info = sap.parse_battery_response(body)
            if info is not None:
                level, charging = info
                print(f"[{ts()}] >>> BATTERY: {level}%{' (charging)' if charging else ''} <<<")
                self.got_battery.set()
                return
        # Other channels (health pings, media, etc.) - just log; ACK already sent.
        msg_id = body[0] & 0x3F if body else -1
        print(f"[{ts()}] RX ch=0x{channel:02x} msg={msg_id} {body.hex(' ')[:48]}")

    # ---- connection ----
    def _on_notify(self, sender, args):
        data = buf_to_bytes(args.characteristic_value)
        # Hop back onto the asyncio loop; WinRT calls this on a threadpool thread.
        self.loop.call_soon_threadsafe(self.queue.put_nowait, data)

    async def run(self):
        print(f"[{ts()}] connecting to {self.mac} ...")
        dev = await BluetoothLEDevice.from_bluetooth_address_async(addr_to_int(self.mac))
        if dev is None:
            print("No bonded device for that address.")
            return 1

        session = await GattSession.from_device_id_async(dev.bluetooth_device_id)
        session.maintain_connection = True

        svc_result = await dev.get_gatt_services_async()
        if svc_result.status != GattCommunicationStatus.SUCCESS:
            print(f"could not read services: {svc_result.status!r} (watch on phone / asleep?)")
            return 1

        notify_char = None
        for service in svc_result.services:
            if str(service.uuid).lower() != SVC_1A1A:
                continue
            ch_result = await service.get_characteristics_async()
            for ch in ch_result.characteristics:
                u = str(ch.uuid).lower()
                if u == CH_NOTIFY:
                    notify_char = ch
                elif u == CH_WRITE:
                    self.write_char = ch

        if notify_char is None or self.write_char is None:
            print("Could not find the SAP characteristics (1a1a service).")
            return 1

        notify_char.add_value_changed(self._on_notify)
        st = await notify_char.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
        print(f"[{ts()}] subscribed to notify: {st == GattCommunicationStatus.SUCCESS}")
        connected = dev.connection_status == BluetoothConnectionStatus.CONNECTED
        print(f"[{ts()}] link CONNECTED={connected}; waiting for band HELLO...\n")

        async def poller():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                if self.handshake_done:
                    await self.request_battery()

        poll_task = asyncio.ensure_future(poller())
        try:
            while True:
                frame = await self.queue.get()
                await self._handle_frame(frame)
                if self.once and self.got_battery.is_set():
                    break
        finally:
            poll_task.cancel()

        print(f"[{ts()}] done.")
        return 0


async def main():
    args = [a for a in sys.argv[1:]]
    once = "--once" in args
    args = [a for a in args if a != "--once"]
    mac = args[0] if args else DEFAULT_ADDRESS
    app = Fit3Battery(mac, once)
    return await app.run()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted.")
