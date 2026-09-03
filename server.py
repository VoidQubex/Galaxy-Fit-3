"""
Galaxy Fit 3 - persistent server.

Stays connected to the band (auto-reconnects), polls information every 10s
(battery, extendable), logs every message the band sends, and dispatches
band-originated commands to your custom action map in actions.py so you can
remap things like "open on phone" / media / find-phone to PC actions.

Usage:
    python server.py                 # run the server (default address)
    python server.py --capture       # verbose: log ALL band messages, incl. repeats
    python server.py 74:19:0A:07:9B:39

Ctrl+C to stop.
"""

import asyncio
import re
import subprocess
import sys
import time
import win32con
import win32api
from datetime import datetime

from winrt.windows.devices.bluetooth import (
    BluetoothLEDevice,
    BluetoothConnectionStatus,
    BluetoothCacheMode,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattSession,
    GattCommunicationStatus,
    GattClientCharacteristicConfigurationDescriptorValue as CCCD,
    GattWriteOption,
)
from winrt.windows.storage.streams import DataReader, DataWriter

import sap
import saft
import bandface
import media
import icons
import volume
import weather
import winnoti
import actions as actions_mod

DEFAULT_ADDRESS = "74:19:0A:07:9B:39"
SVC_1A1A = "00001a1a-0000-1000-8000-00805f9b34fb"
CH_NOTIFY = "797ae4e9-2e58-4fe8-b48d-b5c79599fb9b"
CH_WRITE = "63e30bad-4206-4596-839f-e47cbf7a4b5d"

PHONE_MODEL = "M2007J3SY"
PHONE_VENDOR = "Xiaomi"
POLL_SECONDS = 10
NOTI_POLL_SECONDS = 2       # how often to check Windows for new toasts
RECONNECT_SECONDS = 5
# Pause between big-data chunks. Pacing was never the fix for the big-icon nack
# (the SAP-fragmentation chunk cap was), and it costs real time on every frame -
# so it's off. Raise it only if transfers start nacking.
ICON_CHUNK_DELAY = 0.0
# Write icon chunks without an ATT response: no per-write round trip, so several
# packets fit one connection interval. The band still acks the whole transfer, so
# a dropped chunk shows up as success=False rather than silently corrupting.
ICON_FAST_WRITE = True
# The band answers a media push with another media-state message (msg 1), so
# replying to every one of those loops forever. Rate-limit request-driven pushes;
# pushes we initiate (initial / button / track change) bypass this.
MEDIA_REQUEST_MIN_INTERVAL = 3.0
WEATHER_CACHE_SECONDS = 900     # don't re-hit the weather API more than every 15 min
WEATHER_MIN_INTERVAL = 5.0      # the band re-asks after a push; damp the loop
# Album-art filename announced in now-playing (param 0x04).
# Tested: setting this to an arbitrary name ("ALBUM_ART_1") did NOT make the band
# request the image on ch 0x07/msg 3 the way app icons are fetched - so the art
# trigger is something else. Left empty until the real mechanism is known.
ALBUM_ART_NAME = ""
# Album-art spec, from Samsung's SMediaConstants: the band's exact screen size.
ART_WIDTH, ART_HEIGHT, ART_EXT = 256, 402, "art"
# Package used for --image pushes, so the band keys arbitrary images separately
# from real forwarded apps.
IMAGE_PKG = "pc.custom.image"
# Persistent quick-reply launcher. The band deletes a notification once it's been
# replied to (and the user can swipe it away), which takes the action buttons with
# it - so re-send it whenever it disappears. Same SEQUENCE_ID every time, so it
# updates in place rather than stacking up.
LAUNCHER_SEQ = 9002
LAUNCHER_PKG = "pc.launcher"
LAUNCHER_TITLE = "PC Actions"
LAUNCHER_BODY = "Reply to run an action on the PC"
# After a reply the band shows a ~0.5s spinner then a "Sent." screen, and the
# delete+resend hides behind those. 1.0s was comfortably inside that window but
# added a full second to every press, which hurts for repeated input (mouse
# nudges, typing). 0.45s still lands within the spinner. If the launcher ever
# visibly blinks or a resend gets eaten, raise it back toward 1.0.
LAUNCHER_RESEND_DELAY = 0.45
LAUNCHER_POPUP = False      # no popup card; the launcher lives in the list
# The buzz is ALERT_TYPE, not the popup flag - they're independent. The reference
# hardcodes VIBRATION_ONLY for everything, which is why re-arming buzzed.
LAUNCHER_ALERT = sap.ALERT_SILENT
# Runaway guard: if the band rejects it and deletes instantly, back off instead of
# looping forever at one notification per second.
LAUNCHER_MAX_RESENDS = 5
LAUNCHER_RESEND_WINDOW = 15.0
# Volume range announced to the band (MUSIC_CAPABILITY). Must match the scale we
# report in (volume.BAND_MAX): the band uses Android's 0-15 media-volume scale,
# and a full slider drag sends ~15 steps regardless of what we claim here.
MAX_VOLUME = volume.BAND_MAX
WARNING_VOLUME = volume.BAND_MAX    # == max: never trigger the "high volume" warning
DEDUP_SECONDS = 30      # suppress identical band telemetry lines seen again within this window

CHANNEL_NAMES = {
    0x01: "control", 0x02: "find-band", 0x03: "call", 0x05: "weather",
    0x07: "notification", 0x08: "time-cal", 0x09: "media", 0x0a: "health",
    0x0b: "settings", 0x0c: "debug", 0x0f: "capability", 0x12: "quick-reply",
    0x18: "0x18", 0x1a: "identity",
}


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


def chan_name(ch: int) -> str:
    return CHANNEL_NAMES.get(ch, f"0x{ch:02x}")


class Fit3Server:
    def __init__(self, mac: str, capture: bool, test_notify: bool = False,
                 forward: bool = True, image_path: str = None,
                 image_title: str = "", image_body: str = "",
                 anim_interval: float = 0, frames_path: str = None,
                 art_path: str = None, probe_ft_flag: bool = False,
                 probe_bf_flag: bool = False, set_face_id: int = None,
                 install_face_id: int = None, raw_log: bool = False,
                 probe_capex_flag: bool = False,
                 launcher: bool = True):
        self.mac = mac
        self.capture = capture
        self.test_notify = test_notify
        self.forward = forward
        self.image_path = image_path
        self.image_title = image_title
        self.image_body = image_body
        self.anim_interval = anim_interval
        self.frames_path = frames_path
        self.art_path = art_path
        self.probe_ft_flag = probe_ft_flag
        self.probe_bf_flag = probe_bf_flag
        self.set_face_id = set_face_id
        self.install_face_id = install_face_id
        self.raw_log = raw_log
        self.probe_capex_flag = probe_capex_flag
        self.launcher = launcher
        self._anim_task = None
        self._anim_frame = 0
        self._launcher_task = None
        self._launcher_resends = []   # monotonic times of recent re-sends (runaway guard)
        self.current_menu = ""        # "" = root; set by buttons with "menu": "..."
        self.current_page = actions_mod.menu_start_page("")
        self._last_labels = None       # skip re-sending an unchanged button list
        self._launcher_icon_done = False   # send the launcher icon once, not per re-arm
        self.loop = asyncio.get_event_loop()
        self.state = {"battery": None, "charging": None, "last_battery_at": None}
        # per-connection fields, reset in _setup
        self.seq = 0x40
        self.write_char = None
        self.queue = None
        self.handshake_done = False
        self._recent = {}   # (channel, msg_id, body) -> last-logged monotonic time
        self._action_fired = {}  # (channel, msg_id) -> last-fired monotonic time
        self._last_media = None  # (title, artist, playing) last pushed to the band
        self._media_push_at = 0.0  # monotonic time of the last media push
        self._noti_seq = 0        # SEQUENCE_ID for notifications
        self._bigdata = sap.BigData()   # chunker for oversized ch 0x07 messages
        self._icon_labels = {}    # app_id -> label to draw on its icon
        self._icon_images = {}    # app_id -> PIL logo for that app (real icon)
        self._icons_sent = set()  # icon URLs already delivered this connection
        self._seen_noti = set()   # Windows notification ids already forwarded
        self._seq_to_win = {}     # our SEQUENCE_ID -> Windows notification id
        self._weather = None      # cached weather payload
        self._weather_at = 0.0    # when it was fetched (monotonic)
        self._weather_push_at = 0.0   # last push (monotonic), to break ask->push loops
        self._saft_trans = 0          # SAFT transaction id
        self._saft_event = asyncio.Event()   # signalled by setup-rsp / complete-rsp
        self._saft_setup_ok = False
        self.mtu = 23             # replaced with the negotiated value on connect

    # ---- writes ----
    async def _write(self, frame: bytes, fast: bool = False):
        """Write one SAP frame. `fast` uses write-without-response: no per-write
        round trip, so several packets can ride one connection interval. Only for
        bulk data (icon chunks) - control messages keep the acked write."""
        try:
            if fast:
                status = await self.write_char.write_value_with_option_async(
                    bytes_to_buf(frame), GattWriteOption.WRITE_WITHOUT_RESPONSE)
            else:
                status = await self.write_char.write_value_async(bytes_to_buf(frame))
        except Exception as e:  # noqa: BLE001 - report instead of dying mid-transfer
            print(f"[{ts()}] WRITE EXCEPTION ({len(frame)}B): {e}")
            return False
        ok = status == GattCommunicationStatus.SUCCESS
        if not ok:
            print(f"[{ts()}] WRITE FAILED ({len(frame)}B): {status!r}")
        return ok

    def _next_seq(self) -> int:
        s = self.seq
        self.seq = (self.seq + 0x10) & 0xFF
        if self.seq == 0x00:
            self.seq = 0x40
        return s

    async def _send_message(self, channel: int, body: bytes, fast: bool = False) -> bool:
        # Fragment if the body won't fit one ATT write (e.g. big-data icon chunks).
        for frame in sap.build_frames(channel, self._next_seq(), body, self.mtu):
            if not await self._write(frame, fast=fast):
                return False    # don't keep pushing a transfer that's already broken
        return True

    async def _send_raw(self, frame: bytes):
        await self._write(frame)

    # ---- handshake ----
    async def _on_band_hello(self, frame: bytes):
        bh = sap.parse_band_hello(frame)
        if bh is None:
            return
        print(f"[{ts()}] band HELLO: model='{bh['model']}' vendor='{bh['vendor']}' -> handshaking")
        await self._send_raw(sap.build_phone_hello(bh, sap.random_token(), PHONE_MODEL, PHONE_VENDOR, None))
        await self._send_message(sap.CH_CONTROL, sap.build_init_setting())
        await self._send_message(sap.CH_CONTROL, sap.build_license_request(True))
        self.handshake_done = True
        await self.request_battery()
        # Prime the band with media state so its media screen has data up front.
        await self.push_media_state("initial")
        # Announce our volume range, then the current level, so the band's slider
        # is correct before the user ever touches it.
        await self._send_message(sap.CH_MEDIA,
                                 sap.build_media_capability(MAX_VOLUME, WARNING_VOLUME))
        await self.push_volume("initial")
        # Quick-reply buttons: repurposed as custom action buttons (see actions.py
        # QUICK_REPLIES / QUICK_REPLY_ACTIONS). The band sends the exact button
        # text back when tapped (ch 0x12 NOTIFICATION_ACTION on ch 0x07).
        if actions_mod.QUICK_REPLIES:
            # Enable the feature once; send_launcher() sends the (context-sensitive)
            # button list itself, and re-sends it on every re-arm.
            await self._send_message(sap.CH_QUICK_REPLY,
                                     sap.build_quick_reply_switch(True))
            await self.send_launcher("initial")
        if self.test_notify:
            await asyncio.sleep(1.0)
            await self.send_notification(
                "Hello from your PC", "Reply to test the quick-reply action buttons.",
                app_name="Fit3 Server", pkg="com.example.fit3server",
                need_quick_reply=True)
        if self.image_path:
            await asyncio.sleep(1.0)
            await self.send_image(self.image_path, self.image_title, self.image_body)
        if self.anim_interval:
            await asyncio.sleep(1.0)
            self._anim_task = asyncio.ensure_future(self.animate(self.anim_interval))
        if self.probe_bf_flag:
            await asyncio.sleep(1.5)
            await self.probe_bandface()
        if self.set_face_id is not None:
            await asyncio.sleep(1.5)
            await self.set_watchface(self.set_face_id)
        if self.install_face_id is not None:
            await asyncio.sleep(1.5)
            await self.probe_install(self.install_face_id)
        if self.probe_capex_flag:
            await asyncio.sleep(1.5)
            await self.probe_capex()
        if self.probe_ft_flag:
            await asyncio.sleep(1.5)
            await self.probe_ft()
        if self.art_path:
            await asyncio.sleep(1.5)
            await self.send_album_art(self.art_path)

    async def request_battery(self):
        await self._send_message(sap.CH_SETTINGS, sap.build_battery_request())

    # ---- notifications ----
    async def send_notification(self, title: str, body: str = "", app_name: str = "",
                                pkg: str = "", popup: bool = True, win_id: int = None,
                                seq: int = None, force_icon: bool = False,
                                need_quick_reply: bool = False,
                                alert_type: int = sap.ALERT_VIBRATION_ONLY):
        if seq is None:
            self._noti_seq += 1
            seq = self._noti_seq
        first_push = force_icon or (pkg and pkg not in self._icons_sent)
        payload = sap.build_notification(
            seq, title, body, app_name, pkg,
            icon_newly_added=bool(first_push), popup=popup,
            need_quick_reply=need_quick_reply, alert_type=alert_type)
        if pkg:
            # Remember the label to draw when the band asks for this app's icon.
            self._icon_labels[sap.app_id_for(pkg)] = app_name or title or pkg
        if win_id is not None:
            # So a dismiss on the band can clear the right Windows notification.
            self._seq_to_win[seq] = win_id
        await self._send_message(sap.CH_NOTIFICATION, payload)
        print(f"[{ts()}] notify -> band: {title!r} / {body!r} ({app_name or 'no app'})")

    async def send_launcher(self, reason: str):
        """(Re)create the notification that hosts the custom action buttons.

        Refreshes the button list from actions.current_quick_replies() first, so
        context-sensitive ('when') buttons re-evaluate on every arm. Quick-reply
        lists are mutable live, so this just works without a reconnect.
        """
        if not self.launcher or not actions_mod.QUICK_REPLIES:
            return
        if not self.handshake_done:
            return      # nothing to write to yet; the HELLO path will send it
        labels, page, total = actions_mod.menu_labels(
            self.current_menu, self.current_page)
        self.current_page = page        # store the normalised (wrapped) page
        if not labels:
            print(f"[{ts()}] launcher: menu {self.current_menu!r} has no visible "
                  f"buttons - falling back to root")
            self.current_menu = ""
            self.current_page = actions_mod.menu_start_page("")
            labels, page, total = actions_mod.menu_labels("", self.current_page)

        # Only re-send the button list when it actually changed. Repeatedly
        # tapping the same key (mouse nudges) leaves it identical, and skipping
        # the write saves a message per press.
        if labels != self._last_labels:
            await self._send_message(sap.CH_QUICK_REPLY,
                                     sap.build_quick_reply_list(labels))
            self._last_labels = list(labels)

        # Naming a pkg puts IMAGE_URL in the notification, which is the ONLY
        # reason the band asks for an app icon - and it re-asks on every single
        # re-arm (23 chunks / ~10.8KB per button press). Send it once so the
        # launcher has its icon, then drop it: pure latency after that.
        pkg = LAUNCHER_PKG if not self._launcher_icon_done else ""
        self._launcher_icon_done = True

        title = LAUNCHER_TITLE if not self.current_menu else self.current_menu.title()
        if total == 1:
            body = LAUNCHER_BODY
        elif page == actions_mod.PAGE_INDEX:
            body = f"{total} groups"
        else:
            body = f"Page {page + 1}/{total}"
        await self.send_notification(
            title, body, app_name=LAUNCHER_TITLE,
            pkg=pkg, seq=LAUNCHER_SEQ, popup=LAUNCHER_POPUP,
            need_quick_reply=True, alert_type=LAUNCHER_ALERT)
        if total == 1:
            where = self.current_menu or "root"
        elif page == actions_mod.PAGE_INDEX:
            where = f"{self.current_menu or 'root'} index/{total}"
        else:
            where = f"{self.current_menu or 'root'} p{page + 1}/{total}"
        print(f"[{ts()}] launcher -> band ({reason}) [{where}]; buttons: {labels}")

    def _schedule_launcher_resend(self, reason: str):
        """Put the launcher back shortly after the band removes it."""
        if not self.launcher:
            return
        now = time.monotonic()
        self._launcher_resends = [t for t in self._launcher_resends
                                  if now - t < LAUNCHER_RESEND_WINDOW]
        if len(self._launcher_resends) >= LAUNCHER_MAX_RESENDS:
            print(f"[{ts()}] launcher: {LAUNCHER_MAX_RESENDS} re-sends in "
                  f"{LAUNCHER_RESEND_WINDOW:.0f}s - backing off. The band may be "
                  f"rejecting it (try LAUNCHER_POPUP = True).")
            return
        self._launcher_resends.append(now)
        if self._launcher_task and not self._launcher_task.done():
            self._launcher_task.cancel()
        self._launcher_task = asyncio.ensure_future(self._resend_launcher_later(reason))

    async def _resend_launcher_later(self, reason: str):
        try:
            await asyncio.sleep(LAUNCHER_RESEND_DELAY)
            await self.send_launcher(reason)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - a failed re-arm must not kill the server
            print(f"[{ts()}] launcher re-send failed: {e}")

    async def _forward_windows_notifications(self):
        """Poll Windows toasts and push new ones to the band."""
        ok = await winnoti.request_access()
        if not ok:
            print(f"[{ts()}] notification forwarding OFF: Windows denied access "
                  f"(Settings > Privacy & security > Notifications)")
            return
        # Existing notifications are forwarded too - _seen_noti stops repeats.
        print(f"[{ts()}] forwarding Windows notifications (incl. existing)")

        while True:
            await asyncio.sleep(NOTI_POLL_SECONDS)
            if not self.handshake_done:
                continue
            try:
                current = await winnoti.get_notifications()
            except Exception as e:  # noqa: BLE001
                print(f"[{ts()}] notification read error: {e}")
                continue
            live = {n["id"] for n in current}
            self._seen_noti &= live | self._seen_noti   # keep ids we've seen
            for n in current:
                if n["id"] in self._seen_noti:
                    continue
                self._seen_noti.add(n["id"])
                app_id = sap.app_id_for(n["key"])
                self._icon_labels[app_id] = n["app"]
                img = winnoti.logo_to_image(n["logo_png"])
                if img is not None:
                    self._icon_images[app_id] = img
                await self.send_notification(n["title"], n["body"],
                                             app_name=n["app"], pkg=n["key"],
                                             win_id=n["id"])

    async def send_image(self, path: str, title: str = "", body: str = ""):
        """Put an arbitrary image on the band, as a notification's icon.

        The icon request/response is the only image path that works over BLE
        (album art and watchfaces both go via SAFT, which has no BLE transport).
        The band asks for it at 52px and 112px, so that's the canvas.
        """
        try:
            from PIL import Image
            img = Image.open(path).convert("RGBA")
        except Exception as e:  # noqa: BLE001
            print(f"[{ts()}] could not open image {path!r}: {e}")
            return
        pkg = IMAGE_PKG
        app_id = sap.app_id_for(pkg)
        self._icon_images[app_id] = img
        self._icon_labels[app_id] = title or "Image"
        # Force a re-fetch even if the band cached this icon URL before.
        self._icons_sent.discard("APP_ICON_" + app_id)
        print(f"[{ts()}] sending image {path!r} ({img.size[0]}x{img.size[1]}) as a notification icon")
        await self.send_notification(title or "Image", body, app_name=title or "PC Image", pkg=pkg)

    async def animate(self, interval: float = 6.0):
        """Test: can a notification's icon change AFTER it's been sent?

        Re-sends ONE notification (same SEQUENCE_ID) with IMAGE_NEWLY_ADDED set,
        swapping the served image each time. If the band re-requests the icon and
        updates in place, that's an animated display.
        """
        pkg = IMAGE_PKG
        app_id = sap.app_id_for(pkg)
        seq = 9001                    # fixed id: update, don't stack up new ones
        title = self.image_title or "Animation"
        self._icon_labels[app_id] = title

        loaded = []
        if self.frames_path:
            try:
                loaded = icons.load_frames(self.frames_path)
            except Exception as e:  # noqa: BLE001
                print(f"[{ts()}] could not load frames from {self.frames_path!r}: {e}")
                return
            if not loaded:
                print(f"[{ts()}] no frames found in {self.frames_path!r}")
                return
            print(f"[{ts()}] loaded {len(loaded)} frames from {self.frames_path!r} "
                  f"(~{len(loaded) * interval / 60:.1f} min at {interval}s/frame)")

        # Each frame goes: re-send the notification (same SEQUENCE_ID, with
        # IMAGE_NEWLY_ADDED) -> the band re-fetches the icon -> we answer.
        # TRIED AND REJECTED: pushing an unrequested icon response to the url/size
        # the band last asked for, to skip the notify+request round trip. The band
        # only picks those up sporadically - the icon updates at random - so the
        # request/response handshake is load-bearing, not just latency. ~1fps is
        # the real ceiling.
        frame = 0
        while True:
            img = loaded[frame % len(loaded)] if loaded else icons.render_frame(112, frame)
            self._icon_images[app_id] = img
            self._anim_frame = frame
            self._icons_sent.discard("APP_ICON_" + app_id)
            # popup only on the first frame; later ones update it silently
            await self.send_notification(
                title, f"frame {frame}", app_name=title, pkg=pkg,
                seq=seq, force_icon=True, popup=(frame == 0))
            frame += 1
            await asyncio.sleep(interval)

    async def _on_notification(self, msg_id: int, body: bytes) -> None:
        # The band asking for an app icon. Answer it, or it waits (and shows none).
        if msg_id == sap.MSG_NOTI_ICON:
            req = sap.parse_icon_request(body)
            if req is None:
                print(f"[{ts()}] icon request unparsed: {body.hex(' ')[:60]}")
                return
            url, size = req["url"], req["size"]
            app_id = url.replace("APP_ICON_", "")
            img = self._icon_images.get(app_id)
            if img is not None:
                pixels = icons.to_band_pixels(img, size)      # the app's real logo
            else:
                label = self._icon_labels.get(app_id, "?")
                pixels = icons.render_icon(size, label)        # letter-tile fallback
            resp = sap.build_icon_response(url, size, pixels)
            # Cap each big-data chunk so it fits ONE SAP frame at the negotiated
            # MTU: frame = 6 (framing) + 4 (single hdr) + body. Chunks bigger than
            # this get SAP-fragmented, and large fragmented transfers fail on this
            # band (it nacked at chunk 25/52 with the reference's 980-byte cap).
            cap = min(sap.BIGDATA_CAP, self.mtu - 3 - 6 - 4)
            chunks = self._bigdata.chunk(resp, cap)
            print(f"[{ts()}] icon <- band: {url} size={size} "
                  f"({len(pixels)} px bytes -> {len(chunks)} chunk(s))")
            t0 = time.monotonic()
            for i, c in enumerate(chunks):
                if not await self._send_message(sap.CH_NOTIFICATION, c, fast=ICON_FAST_WRITE):
                    print(f"[{ts()}] icon transfer ABORTED at chunk {i + 1}/{len(chunks)}")
                    return
                if ICON_CHUNK_DELAY and i + 1 < len(chunks):
                    await asyncio.sleep(ICON_CHUNK_DELAY)
            print(f"[{ts()}] icon sent: {len(chunks)} chunks in {time.monotonic() - t0:.2f}s")
            self._icons_sent.add(url)
            return

        # Band dismissed a notification -> clear it on Windows too.
        if msg_id == sap.MSG_NOTI_CLEAR:
            seq = sap.parse_notification_clear(body)
            if seq is None:
                print(f"[{ts()}] clear (unparsed): {body.hex(' ')}")
                return
            if seq == LAUNCHER_SEQ:
                # The launcher only exists to hold the action buttons - put it back.
                print(f"[{ts()}] launcher dismissed on band")
                self._schedule_launcher_resend("re-armed after dismiss")
                return
            win_id = self._seq_to_win.pop(seq, None)
            if win_id is None:
                print(f"[{ts()}] band cleared notification #{seq} (not ours / already gone)")
                return
            ok = winnoti.remove_notification(win_id)
            print(f"[{ts()}] band cleared notification #{seq} -> "
                  f"{'dismissed on Windows' if ok else 'could not dismiss on Windows'}")
            return

        if msg_id == sap.MSG_LARGE_DATA_ACK:
            # [0x3e][version<<3|success][chunk idx: 4 LE]. The reference documents
            # only the first two bytes; the trailing int32 is the last chunk index
            # the band accepted - on failure, where it stopped.
            ack = sap.parse_bigdata_ack(body)
            idx = int.from_bytes(body[2:6], "little") if len(body) >= 6 else None
            print(f"[{ts()}] big-data ack: version={ack['version']} "
                  f"success={ack['success']} chunk_idx={idx}  raw={body.hex(' ')}")
            return

        # Quick-reply button tapped: the band sends the literal button text back
        # (see actions.py QUICK_REPLIES / QUICK_REPLY_ACTIONS).
        if msg_id == sap.MSG_NOTI_ACTION:
            act = sap.parse_notification_action(body)
            if act is None or act.get("text") is None:
                print(f"[{ts()}] quick reply (unparsed): {body.hex(' ')}")
                return
            text = act["text"]
            print(f"[{ts()}] quick reply tapped: {text!r} "
                  f"(menu={self.current_menu or 'root'} page={self.current_page} "
                  f"seq={act['seq']})")

            # Index/leaf paging first - these labels are injected, not config entries.
            jump = actions_mod.menu_index_target(self.current_menu, text)
            if jump is not None and self.current_page == actions_mod.PAGE_INDEX:
                self.current_page = jump
                print(f"[{ts()}]   -> leaf {jump + 1}")
            elif (text == actions_mod.PAGE_BACK
                  and self.current_page >= 0
                  and actions_mod.menu_labels(
                      self.current_menu, actions_mod.PAGE_INDEX)[2] > 1):
                # Leaf Back → index. Config Back on the index still goes to parent.
                self.current_page = actions_mod.PAGE_INDEX
                print(f"[{ts()}]   -> index")
            else:
                action = actions_mod.menu_action(self.current_menu, text)
                if isinstance(action, tuple) and action and action[0] == "__menu__":
                    target = action[1]
                    if actions_mod.menu_exists(target):
                        self.current_menu = target
                        self.current_page = actions_mod.menu_start_page(target)
                        print(f"[{ts()}]   -> menu '{target or 'root'}'")
                    else:
                        print(f"[{ts()}]   -> unknown menu {target!r} (staying put)")
                elif action is None:
                    print(f"[{ts()}]   -> no action mapped for {text!r}")
                else:
                    print(f"[{ts()}]   -> ACTION")
                    self._run_action(action, {"channel": sap.CH_NOTIFICATION,
                                              "msg_id": msg_id, "body": body,
                                              "text": text, "seq": act["seq"]})
            return

        if self.capture:
            print(f"[{ts()}] noti msg={msg_id} body={body.hex(' ')[:60]}")

    async def probe_bandface(self):
        """Query the watchface channel: what's installed, what's active.

        Read-only - no install, no delete. Dumps raw replies so the list/edit
        payload layouts (which jadx couldn't fully decompile) can be decoded
        from real data.
        """
        print(f"[{ts()}] === bandface probe (read-only) ===")
        for label, payload in (
            ("GET_BANDFACE_LIST", bandface.get_bandface_list()),
            ("GET_CURRENT_BANDFACE", bandface.get_current_bandface()),
            ("GET_CURRENT_ORDER", bandface.get_current_order()),
            ("GET_ALL_INFO", bandface.get_all_info()),
        ):
            print(f"[{ts()}] BANDFACE -> {label}: {payload.hex(' ')}")
            await self._send_message(bandface.CHANNEL, payload)
            await asyncio.sleep(2.5)
        print(f"[{ts()}] === bandface probe sent ===")

    async def set_watchface(self, wf_id: int, sampler_id: int = 0):
        """Switch the band's active watchface (SET_CURRENT_BANDFACE, ch 0x06)."""
        payload = bandface.set_current_bandface(wf_id, sampler_id)
        print(f"[{ts()}] BANDFACE -> SET_CURRENT_BANDFACE id={wf_id} "
              f"sampler={sampler_id}: {payload.hex(' ')}")
        await self._send_message(bandface.CHANNEL, payload)
        await asyncio.sleep(2.0)
        # Read it back so the log proves the switch took.
        await self._send_message(bandface.CHANNEL, bandface.get_current_bandface())

    async def probe_install(self, wf_id: int):
        """Send INSTALL_BANDFACE alone and watch what the band does.

        The real plugin ships the .bin over SAFT (appId 6) and sends this; doing
        it without the file tells us whether the band drives the transfer - if it
        opens a file-transfer session or asks for anything, that's the way in.
        """
        print(f"[{ts()}] === INSTALL_BANDFACE probe (id={wf_id}) ===")
        payload = bandface.install_bandface(wf_id)
        print(f"[{ts()}] BANDFACE -> INSTALL_BANDFACE: {payload.hex(' ')}")
        await self._send_message(bandface.CHANNEL, payload)
        print(f"[{ts()}] watching for a reply / any file-transfer activity ...")

    async def probe_capex(self):
        """Sweep a SAP capability-discovery query across candidate sessions.

        We don't know which session carries capability traffic (the framework
        tracks that connection under the reserved id 65535/65535, which isn't a
        session number), so try the plausible ones. A reply would enumerate the
        band's services and their acceptor ids - the missing input for opening
        SAFT. Run with --raw to catch anything that doesn't parse as a normal
        data frame.
        """
        print(f"[{ts()}] === SAP capability-exchange probe ===")
        broad = saft.build_capability_query()
        targeted = saft.build_capability_query([saft.PROFILE_FILETRANSFER])
        print(f"[{ts()}]   broad query    = {broad.hex(' ')}")
        print(f"[{ts()}]   targeted query = {targeted.hex(' ')}")
        for ch in (0, 1, 255, 0x0F):
            for label, q in (("broad", broad), ("targeted", targeted)):
                print(f"[{ts()}] capex -> session {ch} ({label})")
                await self._send_message(ch, q)
                await asyncio.sleep(2.0)
        print(f"[{ts()}] === capex probe sent; watching for a response ===")

    async def probe_ft(self):
        """Blind shot: ask the band to open /system/filetransfer.

        We don't know the real acceptorId (it comes from SAP capability
        discovery) or which session carries service-connection messages, so try a
        few plausible combinations and log anything that comes back. A rejection
        is a win - it means something is listening.
        """
        print(f"[{ts()}] === FT service-connection probe ===")
        # Candidate control sessions to send the request on.
        for ctrl in (0, 1):
            for acceptor in (0, 1):
                req = saft.build_service_connection_request(
                    acceptor_id=acceptor, initiator_id=1,
                    profile_id=saft.PROFILE_FILETRANSFER,
                    channels=saft.ft_channels(saft.CH_COMMAND, saft.CH_DATA))
                print(f"[{ts()}] probe: ctrl_session={ctrl} acceptorId={acceptor} "
                      f"({len(req)}B) {req[:12].hex(' ')}...")
                await self._send_message(ctrl, req)
                await asyncio.sleep(2.5)
        print(f"[{ts()}] === probe sent; watching for any reply ===")

    # ---- SAFT file transfer (album art) ----
    async def _on_saft(self, channel: int, body: bytes) -> None:
        msg = saft.parse_command(body)
        if msg is None:
            print(f"[{ts()}] SAFT ch{channel} (non-JSON, {len(body)}B): {body.hex(' ')[:48]}")
            return
        print(f"[{ts()}] SAFT <- band: {msg}")
        kind = msg.get("msgId", "")
        if kind == saft.SETUP_RSP:
            self._saft_setup_ok = (msg.get("result") == saft.RESULT_SUCCESS)
            self._saft_event.set()
        elif kind == saft.COMPLETE_RSP:
            self._saft_event.set()

    async def send_file(self, data: bytes, file_name: str) -> bool:
        """Push a file to the band over SAFT. Returns True if it completed."""
        self._saft_trans += 1
        cap = self.mtu - 3 - 6 - 4      # keep every packet inside one SAP frame
        packets = saft.data_packets(data, cap)
        print(f"[{ts()}] SAFT -> setup-req: {file_name!r} {len(data)}B "
              f"({len(packets)} data packets)")

        self._saft_event.clear()
        self._saft_setup_ok = False
        await self._send_message(saft.CH_COMMAND, saft.build_setup_req(
            self._saft_trans, file_name, len(data),
            file_path=file_name, last_modified_ms=int(time.time() * 1000)))

        try:
            await asyncio.wait_for(self._saft_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            print(f"[{ts()}] SAFT: no setup-rsp within 10s "
                  f"(band may not run the file-transfer service on this link)")
            return False
        if not self._saft_setup_ok:
            print(f"[{ts()}] SAFT: band REJECTED the setup")
            return False

        print(f"[{ts()}] SAFT: setup accepted, sending {len(data)}B ...")
        t0 = time.monotonic()
        for p in packets:
            if not await self._send_message(saft.CH_DATA, p, fast=True):
                print(f"[{ts()}] SAFT: data write failed")
                return False
        print(f"[{ts()}] SAFT: data sent in {time.monotonic() - t0:.2f}s, completing")

        self._saft_event.clear()
        await self._send_message(saft.CH_COMMAND,
                                 saft.build_ctrl_req(saft.COMPLETE_REQ, file_name))
        try:
            await asyncio.wait_for(self._saft_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            print(f"[{ts()}] SAFT: no complete-rsp (file may still have landed)")
            return False
        return True

    async def send_album_art(self, path: str):
        """Scale an image to the band's album-art spec, ship it via SAFT, then
        name it in a now-playing push so the media screen picks it up.

        Spec from Samsung's SMediaConstants: 256x402 PNG, quality 50, ".art".
        """
        import io
        from PIL import Image
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:  # noqa: BLE001
            print(f"[{ts()}] could not open art {path!r}: {e}")
            return
        img = img.resize((ART_WIDTH, ART_HEIGHT), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()

        name = f"pcart{int(time.time())}.{ART_EXT}"
        print(f"[{ts()}] album art: {path!r} -> {ART_WIDTH}x{ART_HEIGHT} PNG, "
              f"{len(data)}B, as {name!r}")
        if not await self.send_file(data, name):
            print(f"[{ts()}] album art: SAFT transfer did not complete")
            return
        print(f"[{ts()}] album art: file delivered; announcing it in now-playing")
        state = await media.get_media_state()
        await self._send_message(sap.CH_MEDIA, sap.build_now_playing(
            state["title"], state["artist"], state["duration_ms"], name, state["app"]))
        await self._send_message(sap.CH_MEDIA, sap.build_playback_state(
            state["playing"], state["position_ms"], 0, state["app"]))

    # ---- weather ----
    async def push_weather(self, reason: str):
        """Send weather to the band. Cached - the band asks repeatedly."""
        now = time.monotonic()
        if self._weather is None or (now - self._weather_at) > WEATHER_CACHE_SECONDS:
            try:
                # Network call: keep it off the event loop.
                self._weather = await asyncio.get_event_loop().run_in_executor(
                    None, weather.get_weather)
                self._weather_at = now
            except Exception as e:  # noqa: BLE001 - no weather is not fatal
                print(f"[{ts()}] weather fetch failed: {e}")
                return
        w = self._weather
        body = sap.build_weather(w, uv_text=weather.uv_label(w["uv"]))
        ok = await self._send_message(sap.CH_WEATHER, body)
        print(f"[{ts()}] weather -> band ({len(body)}B): {weather.summary(w)}  ({reason})"
              + ("" if ok else "  [SEND FAILED]"))

    async def _on_weather(self, msg_id: int, body: bytes) -> None:
        # 0x01 = the band asking for a refresh; 0x00 = add-current-location.
        if msg_id in (sap.MSG_WEATHER_INFO, sap.MSG_WEATHER_ADD_LOCATION):
            if (time.monotonic() - self._weather_push_at) < WEATHER_MIN_INTERVAL:
                return      # it re-asks after our push; don't loop like media did
            self._weather_push_at = time.monotonic()
            await self.push_weather("band asked")
            return
        if self.capture:
            print(f"[{ts()}] weather msg={msg_id} body={body.hex(' ')[:48]}")

    # ---- media ----
    async def push_media_state(self, reason: str, force: bool = True):
        """Send now-playing + playback state to the band.

        The band's media screen blocks waiting for these after it asks, so always
        answer - with media.IDLE if nothing is playing.
        """
        state = await media.get_media_state()
        snapshot = (state["title"], state["artist"], state["playing"])
        if not force and snapshot == self._last_media:
            return          # unchanged; don't spam the band
        self._last_media = snapshot
        self._media_push_at = time.monotonic()

        # EXPERIMENT: param 0x04 is the album-art *filename*. If we name one, the
        # band should come back asking for that image on ch 0x07 / msg 3 (the same
        # request/response the notification-icon flow uses).
        await self._send_message(sap.CH_MEDIA, sap.build_now_playing(
            state["title"], state["artist"], state["duration_ms"],
            ALBUM_ART_NAME, state["app"]))
        await self._send_message(sap.CH_MEDIA, sap.build_playback_state(
            state["playing"], state["position_ms"], 0, state["app"]))
        print(f"[{ts()}] media -> band: {media.summary(state)}  ({reason})")

    async def push_volume(self, reason: str):
        pct = volume.get_volume()
        if pct < 0:
            return
        # Report in BAND units (0..BAND_MAX), not percent - that's the scale its
        # slider actually uses. Muted reads as 0, which is what a user expects.
        units = volume.get_units()
        await self._send_message(sap.CH_MEDIA, sap.build_media_volume(units))
        print(f"[{ts()}] volume -> band: {units}/{volume.BAND_MAX}  (PC {pct}%)  ({reason})")

    async def _on_media(self, msg_id: int, body: bytes) -> None:
        # The band asking for media state - answer it or its UI hangs. But it also
        # emits this in reply to our own pushes, so answering every one is an
        # infinite loop: only answer if we haven't just pushed.
        if msg_id in (sap.MSG_MEDIA_STATE_REQ_A, sap.MSG_MEDIA_STATE_REQ_B):
            if (time.monotonic() - self._media_push_at) < MEDIA_REQUEST_MIN_INTERVAL:
                return
            await self.push_media_state("band asked")
            return

        # "What's your volume range?" -> tells the band how to scale its slider.
        if msg_id == sap.MSG_MEDIA_CAPABILITY_REQ:
            await self._send_message(sap.CH_MEDIA,
                                     sap.build_media_capability(MAX_VOLUME, WARNING_VOLUME))
            print(f"[{ts()}] capability -> band: maxVolume={MAX_VOLUME} "
                  f"warningVolume={WARNING_VOLUME}")
            return

        # "What's your volume?" - without this the band's slider is guessing.
        if msg_id == sap.MSG_MEDIA_VOLUME_REQ:
            await self.push_volume("band asked")
            return

        if msg_id == sap.MSG_MEDIA_VOLUME:
            # Shows the volume popup
            win32api.keybd_event(win32con.VK_VOLUME_UP, 0)
            win32api.keybd_event(win32con.VK_VOLUME_UP, 0, win32con.KEYEVENTF_KEYUP)
            time.sleep(0.05)  # give the OS a moment to register the first key event            
            win32api.keybd_event(win32con.VK_VOLUME_DOWN, 0)
            win32api.keybd_event(win32con.VK_VOLUME_DOWN, 0, win32con.KEYEVENTF_KEYUP)

            v = sap.parse_media_volume(body)
            if v is None:
                return
            cmd, val = v["command"], v["value"]
            name = sap.VOL_CMD_NAMES.get(cmd, f"cmd{cmd}")
            if cmd == sap.VOL_SET:
                # The value is in band units, matching the maxVolume we announced.
                ok = volume.set_volume(volume.units_to_pct(val))
                now = volume.get_volume()
            elif cmd in (sap.VOL_UP, sap.VOL_DOWN):
                now = volume.step(cmd == sap.VOL_UP)
            elif cmd in (sap.VOL_MUTE_ON, sap.VOL_MUTE_OFF):
                volume.set_mute(cmd == sap.VOL_MUTE_ON)
                now = volume.get_volume()
            else:
                print(f"[{ts()}] volume: unknown command {cmd} (value={val}) "
                      f"body={body.hex(' ')}")
                return
            muted = " (muted)" if volume.is_muted() else ""
            print(f"[{ts()}] volume: {name}"
                  + (f" {val}" if cmd == sap.VOL_SET else "")
                  + f" -> PC now {now}%{muted}")
            # Echo the real level back so the band's slider tracks the PC.
            await self.push_volume("after change")
            return

        if msg_id == sap.MSG_MEDIA_KEY:
            btn = sap.parse_media_button(body)
            if btn is None:
                return
            name = sap.MEDIA_KEY_NAMES.get(btn["key_code"], f"key{btn['key_code']}")
            # One tap = a PRESSED and a RELEASED event. Act on PRESSED only.
            if btn["key_action"] != sap.ACTION_PRESSED:
                if self.capture:
                    print(f"[{ts()}] media button: {name} released (ignored)")
                return
            print(f"[{ts()}] media button: {name} pressed")
            self._maybe_fire((sap.CH_MEDIA, msg_id),
                             {"channel": sap.CH_MEDIA, "msg_id": msg_id, "body": body,
                              "is_response": False, "key_code": btn["key_code"],
                              "key_name": name})
            # Let the action take effect, then refresh what the band shows.
            await asyncio.sleep(0.4)
            await self.push_media_state(f"after {name}")
            return

        if self.capture:
            print(f"[{ts()}] media msg={msg_id} body={body.hex(' ')[:48]}")

    # ---- receive ----
    async def _handle_frame(self, frame: bytes):
        # --raw: dump every inbound frame BEFORE any parsing. We're the phone side
        # of this link, so this is a full capture of what the band actually sends -
        # including frames the normal path drops silently (CRC-fail / unknown type),
        # which is where any session or capability traffic would be hiding.
        if self.raw_log:
            hdr = frame[0] if frame else 0
            ftype = "CONTROL" if (hdr & 0x10) else "data"
            print(f"[{ts()}] RAW <- {len(frame):>4}B hdr=0x{hdr:02x} ({ftype}) {frame.hex(' ')[:200]}")

        if sap.frame_is_hello(frame):
            if sap.hello_is_band(frame):
                await self._on_band_hello(frame)
            return
        payload = sap.frame_unwrap(frame)
        if payload is None:
            if self.raw_log:
                print(f"[{ts()}]   ^ DROPPED: failed CRC/length unwrap")
            return
        pkt = sap.packet_parse(payload)
        kind = pkt["kind"]
        if kind == "unknown" and self.raw_log:
            print(f"[{ts()}]   ^ DROPPED: unknown packet type 0x{payload[0]:02x} "
                  f"payload={payload.hex(' ')[:120]}")
        if kind == "ack":
            return
        if kind in ("single", "first"):
            await self._send_raw(sap.frame_wrap(sap.packet_encode_ack(pkt["seq"], kind == "first")))
            await self._dispatch(pkt["channel"], pkt["body"])
        elif kind == "cont":
            await self._send_raw(sap.frame_wrap(sap.packet_encode_ack(pkt["seq"], True)))

    async def _dispatch(self, channel: int, body: bytes):
        if not body:
            return
        msg_id = body[0] & 0x3F
        is_response = bool((body[0] >> 6) & 1)

        # Media, weather and notifications need real replies, not just an action.
        if channel == sap.CH_MEDIA:
            await self._on_media(msg_id, body)
            return
        if channel == bandface.CHANNEL:
            print(f"[{ts()}] BANDFACE <- {bandface.describe(body)}")
            return
        if channel == sap.CH_WEATHER:
            await self._on_weather(msg_id, body)
            return
        if channel in (saft.CH_COMMAND, saft.CH_DATA):
            await self._on_saft(channel, body)
            return
        if channel == sap.CH_NOTIFICATION and msg_id in (
                sap.MSG_NOTI_ICON, sap.MSG_NOTI_CLEAR, sap.MSG_LARGE_DATA_ACK,
                sap.MSG_NOTI_ACTION):
            await self._on_notification(msg_id, body)
            return

        # Update known telemetry.
        if channel == sap.CH_SETTINGS:
            info = sap.parse_battery_response(body)
            if info is not None:
                level, charging = info
                self.state.update(battery=level, charging=charging, last_battery_at=time.time())
                print(f"[{ts()}] battery: {level}%{' (charging)' if charging else ''}")
                return

        # A band-originated command (request-typed) is what we remap. Show it
        # prominently and fire any mapped action.
        key = (channel, msg_id)
        ctx = {"channel": channel, "msg_id": msg_id, "is_response": is_response, "body": body}

        if key in actions_mod.ACTIONS:
            print(f"[{ts()}] CMD  ch=0x{channel:02x} ({chan_name(channel)}) msg={msg_id}  "
                  f"body={body.hex(' ')}")
            self._maybe_fire(key, ctx)
            return

        # Otherwise just log. Dedup repetitive telemetry unless --capture.
        tag = "CMD " if not is_response else "resp"
        if self.capture or self._should_log(channel, msg_id, body):
            print(f"[{ts()}] {tag} ch=0x{channel:02x} ({chan_name(channel)}) msg={msg_id}  "
                  f"body={body.hex(' ')[:60]}")

    def _maybe_fire(self, key, ctx):
        """Run the mapped action for `key`, honouring actions.DEBOUNCE.

        Keys absent from DEBOUNCE fire on every call.
        """
        action = actions_mod.ACTIONS.get(key)
        if action is None:
            return
        window = getattr(actions_mod, "DEBOUNCE", {}).get(key, 0)
        if window:
            now = time.monotonic()
            last = self._action_fired.get(key)
            if last is not None and (now - last) < window:
                print(f"[{ts()}]   -> debounced ({window}s)")
                return
            self._action_fired[key] = now
        print(f"[{ts()}]   -> ACTION")
        self._run_action(action, ctx)

    def _action_notify(self, title: str, body: str = "", app_name: str = "PC",
                       alert: bool = True, popup: bool = True):
        """Thread-safe: let an action push a notification back to the band.

        Actions run on the event-loop thread (or a worker thread they spawn);
        this schedules the async send onto the loop from wherever it's called.
        Exposed to every action as ctx['notify'].
        """
        pkg = "pc." + "".join(c if c.isalnum() else "_" for c in app_name.lower())
        coro = self.send_notification(
            title, body, app_name=app_name, pkg=pkg, popup=popup,
            alert_type=sap.ALERT_VIBRATION_ONLY if alert else sap.ALERT_SILENT)
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception as e:  # noqa: BLE001
            print(f"[{ts()}] action notify failed: {e}")

    def _run_action(self, action, ctx):
        ctx.setdefault("notify", self._action_notify)   # ctx['notify'](title, body, ...)
        try:
            if callable(action):
                action(ctx)
            elif isinstance(action, str):
                # Fully detached: a shell=True child otherwise shares this
                # process's console, so closing/crashing it can take the server
                # down too.
                subprocess.Popen(
                    action, shell=True, close_fds=True,
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                print(f"[{ts()}] (action for {ctx['channel']:#x}/{ctx['msg_id']} has unsupported type)")
        except Exception as e:  # noqa: BLE001 - never let an action kill the server
            print(f"[{ts()}] action error: {e}")

    def _should_log(self, channel: int, msg_id: int, body: bytes) -> bool:
        now = time.monotonic()
        key = (channel, msg_id, bytes(body))
        last = self._recent.get(key)
        self._recent[key] = now
        # prune occasionally
        if len(self._recent) > 256:
            self._recent = {k: v for k, v in self._recent.items() if now - v < DEDUP_SECONDS}
        return last is None or (now - last) >= DEDUP_SECONDS

    # ---- notify callback (threadpool thread) ----
    def _on_notify(self, sender, args):
        data = buf_to_bytes(args.characteristic_value)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, ("frame", data))

    def _on_conn_changed(self, dev):
        def handler(sender, args):
            if dev.connection_status != BluetoothConnectionStatus.CONNECTED:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, ("disconnected", None))
        return handler

    # ---- one connection lifecycle ----
    async def _serve_once(self) -> None:
        # Reset per-connection state.
        self.seq = 0x40
        self.write_char = None
        self.handshake_done = False
        self._last_media = None     # force a fresh media push on this connection
        self._media_push_at = 0.0
        self._launcher_resends = []  # fresh link, fresh runaway budget
        self.current_menu = ""
        self.current_page = actions_mod.menu_start_page("")  # new link starts at root
        self._last_labels = None
        self._launcher_icon_done = False
        self.queue = asyncio.Queue()

        print(f"[{ts()}] connecting to {self.mac} ...")
        dev = await BluetoothLEDevice.from_bluetooth_address_async(addr_to_int(self.mac))
        if dev is None:
            raise RuntimeError("no bonded device for that address")

        session = await GattSession.from_device_id_async(dev.bluetooth_device_id)
        session.maintain_connection = True
        dev.add_connection_status_changed(self._on_conn_changed(dev))
        self._session = session   # keep alive; also the source of the MTU

        # WinRT sometimes returns a cached, characteristic-less view of the GATT
        # DB right after connect; force an UNCACHED read and retry until the SAP
        # characteristics show up.
        notify_char = None
        for attempt in range(6):
            svc_result = await dev.get_gatt_services_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
            if svc_result.status != GattCommunicationStatus.SUCCESS:
                await asyncio.sleep(1.0)
                continue
            for service in svc_result.services:
                if str(service.uuid).lower() != SVC_1A1A:
                    continue
                chs = await service.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
                for ch in chs.characteristics:
                    u = str(ch.uuid).lower()
                    if u == CH_NOTIFY:
                        notify_char = ch
                    elif u == CH_WRITE:
                        self.write_char = ch
            if notify_char is not None and self.write_char is not None:
                break
            await asyncio.sleep(1.0)
        if notify_char is None or self.write_char is None:
            raise RuntimeError("SAP characteristics (1a1a) not found")

        notify_char.add_value_changed(self._on_notify)
        st = await notify_char.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
        if st != GattCommunicationStatus.SUCCESS:
            raise RuntimeError(f"subscribe failed: {st!r}")
        try:
            self.mtu = int(session.max_pdu_size)
        except Exception:  # noqa: BLE001 - fall back to the safe minimum
            self.mtu = 23
        print(f"[{ts()}] connected & subscribed; MTU={self.mtu}; polling every {POLL_SECONDS}s. "
              f"Waiting for band HELLO...")

        async def poller():
            while True:
                await asyncio.sleep(POLL_SECONDS)
                if not self.handshake_done:
                    continue
                await self.request_battery()
                # Keep the band's now-playing fresh, but only when it changed.
                await self.push_media_state("track changed", force=False)

        poll_task = asyncio.ensure_future(poller())
        noti_task = (asyncio.ensure_future(self._forward_windows_notifications())
                     if self.forward else None)
        try:
            while True:
                what, data = await self.queue.get()
                if what == "disconnected":
                    print(f"[{ts()}] link dropped.")
                    return
                await self._handle_frame(data)
        finally:
            poll_task.cancel()
            if noti_task is not None:
                noti_task.cancel()
            if self._launcher_task is not None and not self._launcher_task.done():
                self._launcher_task.cancel()   # don't re-arm onto a dead link

    async def run(self) -> int:
        print(f"[{ts()}] Fit 3 server starting (capture={self.capture}). Ctrl+C to stop.")
        while True:
            try:
                await self._serve_once()
            except Exception as e:  # noqa: BLE001
                print(f"[{ts()}] connection error: {e}")
            print(f"[{ts()}] reconnecting in {RECONNECT_SECONDS}s...\n")
            await asyncio.sleep(RECONNECT_SECONDS)


def _opt(args, name, default=None):
    """Pull '--name value' out of args, returning (value, remaining_args)."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
    return default, args


async def main() -> int:
    args = sys.argv[1:]
    capture = "--capture" in args
    test_notify = "--test-notify" in args
    forward = "--no-forward" not in args
    probe = "--probe-ft" in args
    probe_bf = "--probe-bandface" in args
    raw_log = "--raw" in args
    probe_capex = "--probe-capex" in args
    launcher = "--no-launcher" not in args
    args = [a for a in args
            if a not in ("--capture", "--test-notify", "--no-forward", "--probe-ft",
                         "--probe-bandface", "--raw", "--probe-capex", "--no-launcher")]
    image, args = _opt(args, "--image")
    title, args = _opt(args, "--title", "Image")
    body, args = _opt(args, "--body", "")
    set_face, args = _opt(args, "--set-face")
    set_face_id = int(set_face, 0) if set_face else None
    inst_face, args = _opt(args, "--probe-install")
    install_face_id = int(inst_face, 0) if inst_face else None
    anim, args = _opt(args, "--animate")
    frames, args = _opt(args, "--frames")
    art, args = _opt(args, "--art")
    anim_interval = float(anim) if anim else (3.0 if frames else 0)
    mac = args[0] if args else DEFAULT_ADDRESS
    # Fail fast on a bad address rather than reconnect-looping forever on it.
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        print(f"Not a Bluetooth address: {mac!r}\n"
              f"Usage: python server.py [AA:BB:CC:DD:EE:FF] [--image PATH "
              f"--title T --body B] [--capture] [--test-notify] [--no-forward]\n"
              f"                        [--frames PATH --animate SECS] [--no-launcher]\n"
              f"(quote multi-word --title/--body values)")
        return 2
    return await Fit3Server(mac, capture, test_notify, forward, image, title, body,
                            anim_interval, frames, art, probe, probe_bf,
                            set_face_id, install_face_id, raw_log, probe_capex, launcher).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
