"""
Samsung Accessory Protocol (SAP) for the Galaxy Fit 3 - transport, session and
payload layers, ported to Python from HighwayStar's Gadgetbridge samsung-fit3
branch (AGPL-3.0). Pure bytes in / bytes out; no BLE here.

Layers:
  transport  SapFrame   [LEN:2 BE][HDR_CRC:2 BE][PAYLOAD][DATA_CRC:2 BE]  (CRC-16/ARC)
  handshake  SapHello   raw 0x68... frames, NOT CRC-wrapped
  session    SapPacket  [TYPE][SEQ][CHAN][CHAN][BODY]  + ACKs
  message    build_*    [HDR][params]   HDR: bit7 variable, bit6 response, bits5-0 msg id
"""

import os
import time


# --------------------------------------------------------------------------
# CRC-16/ARC (a.k.a. CRC-16/ANSI, IBM): poly 0x8005 reflected -> 0xA001, init 0.
# --------------------------------------------------------------------------
def crc16_arc(data: bytes, init: int = 0) -> int:
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


# --------------------------------------------------------------------------
# Transport frame
# --------------------------------------------------------------------------
def frame_wrap(payload: bytes) -> bytes:
    ln = len(payload)
    lenb = bytes([(ln >> 8) & 0xFF, ln & 0xFF])
    hdr = crc16_arc(lenb)
    dcrc = crc16_arc(payload)
    return lenb + bytes([(hdr >> 8) & 0xFF, hdr & 0xFF]) + payload + bytes([(dcrc >> 8) & 0xFF, dcrc & 0xFF])


def frame_is_hello(frame: bytes) -> bool:
    return len(frame) >= 1 and frame[0] == 0x68


def frame_unwrap(frame: bytes):
    """Return payload bytes, or None if malformed / CRC mismatch."""
    if frame is None or len(frame) < 6:
        return None
    ln = (frame[0] << 8) | frame[1]
    if len(frame) != ln + 6:
        return None
    hdr_crc = (frame[2] << 8) | frame[3]
    if crc16_arc(frame[0:2]) != hdr_crc:
        return None
    data_crc = (frame[4 + ln] << 8) | frame[5 + ln]
    if crc16_arc(frame[4:4 + ln]) != data_crc:
        return None
    return frame[4:4 + ln]


# --------------------------------------------------------------------------
# HELLO handshake (fixed 105-byte layout, sent raw)
# --------------------------------------------------------------------------
_PHONE_HEADER = bytes([0x68, 0x15, 0x04, 0x01, 0x00, 0xFF, 0x03])
_HELLO_LEN = 105
_PARAM_OFF, _PARAM_LEN = 43, 7
_MODEL_OFF, _VENDOR_OFF, _VENDOR2_OFF, _FIELD_LEN = 50, 66, 82, 16
_ADDR_OFF, _ADDR_LEN, _TRAILER_OFF = 98, 6, 104


def hello_is_band(frame: bytes) -> bool:
    return frame_is_hello(frame) and len(frame) >= 2 and frame[1] == 0x14


def _get_field(f: bytes, off: int) -> str:
    end = off
    limit = off + _FIELD_LEN
    while end < limit and f[end] != 0:
        end += 1
    return f[off:end].decode("ascii", errors="replace")


def parse_band_hello(frame: bytes):
    """Return dict(param_block, address, model, vendor) or None."""
    if not hello_is_band(frame) or len(frame) < _HELLO_LEN:
        return None
    return {
        "param_block": frame[_PARAM_OFF:_PARAM_OFF + _PARAM_LEN],
        "address": frame[_ADDR_OFF:_ADDR_OFF + _ADDR_LEN],
        "model": _get_field(frame, _MODEL_OFF),
        "vendor": _get_field(frame, _VENDOR_OFF),
    }


def _put_field(f: bytearray, off: int, value: str) -> None:
    b = value.encode("ascii", errors="replace")
    n = min(len(b), _FIELD_LEN - 1)   # keep a NUL terminator inside the field
    f[off:off + n] = b[:n]


def random_token() -> str:
    """32 upper-case hex chars (a session id, not a security key)."""
    return os.urandom(16).hex().upper()


def build_phone_hello(band_hello: dict, token32hex: str, phone_model: str,
                      phone_vendor: str, device_address: bytes = None) -> bytes:
    f = bytearray(_HELLO_LEN)
    f[0:len(_PHONE_HEADER)] = _PHONE_HEADER
    sap = ("SAP_" + token32hex).encode("ascii")
    f[7:7 + min(len(sap), 36)] = sap[:36]
    f[_PARAM_OFF:_PARAM_OFF + _PARAM_LEN] = band_hello["param_block"]
    _put_field(f, _MODEL_OFF, phone_model)
    _put_field(f, _VENDOR_OFF, phone_vendor)
    _put_field(f, _VENDOR2_OFF, phone_vendor)
    if device_address is not None and len(device_address) == _ADDR_LEN:
        f[_ADDR_OFF:_ADDR_OFF + _ADDR_LEN] = device_address
    f[_TRAILER_OFF] = 0x02
    return bytes(f)


# --------------------------------------------------------------------------
# Session packets
# --------------------------------------------------------------------------
TYPE_SINGLE = 0x01
TYPE_FIRST = 0x03
TYPE_CONT = 0x07
TYPE_ACK = 0x88
ACK_FLAG_SINGLE = 0x80
ACK_FLAG_CHUNK = 0xC0


def packet_encode_single(channel: int, seq: int, body: bytes) -> bytes:
    return bytes([TYPE_SINGLE, seq & 0xFF, channel & 0xFF, channel & 0xFF]) + body


def packet_encode_first_fragment(channel: int, seq: int, chunk: bytes) -> bytes:
    return bytes([TYPE_FIRST, seq & 0xFF, channel & 0xFF, channel & 0xFF]) + chunk


def packet_encode_continuation(seq: int, chunk: bytes) -> bytes:
    return bytes([TYPE_CONT, seq & 0xFF]) + chunk


def packet_encode_ack(seq: int, chunk: bool) -> bytes:
    return bytes([TYPE_ACK, seq & 0xFF, ACK_FLAG_CHUNK if chunk else ACK_FLAG_SINGLE, 0x00])


def build_frames(channel: int, seq: int, body: bytes, mtu: int):
    """Frames to send `body` on `channel`, fragmenting if it exceeds the MTU.

    One SINGLE frame if it fits, else one FIRST fragment (carries the channel)
    plus as many CONTINUATION frames as needed. A continuation's SEQ is the
    chunk's SEQ with bit 0 set.
    """
    max_frame = mtu - 3
    single = frame_wrap(packet_encode_single(channel, seq, body))
    if len(single) <= max_frame:
        return [single]

    first_cap = max_frame - 6 - 4    # framing(6) + [TYPE][SEQ][CHAN][CHAN]
    cont_cap = max_frame - 6 - 2     # framing(6) + [TYPE][SEQ]
    if first_cap <= 0 or cont_cap <= 0:
        raise ValueError(f"MTU too small to fragment ({mtu})")

    frames = []
    off = min(first_cap, len(body))
    frames.append(frame_wrap(packet_encode_first_fragment(channel, seq, body[:off])))
    cont_seq = seq | 0x01
    while off < len(body):
        end = min(off + cont_cap, len(body))
        frames.append(frame_wrap(packet_encode_continuation(cont_seq, body[off:end])))
        off = end
    return frames


def packet_parse(payload: bytes):
    """Return dict(kind, seq, channel, body). kind in single/first/cont/ack/unknown."""
    if payload is None or len(payload) < 2:
        return {"kind": "unknown", "seq": -1, "channel": -1, "body": b""}
    t = payload[0]
    seq = payload[1]
    if t == TYPE_ACK:
        return {"kind": "ack", "seq": seq, "channel": -1, "body": b""}
    if t & 0x04:  # continuation
        return {"kind": "cont", "seq": seq, "channel": -1, "body": payload[2:]}
    if len(payload) < 4:
        return {"kind": "unknown", "seq": seq, "channel": -1, "body": b""}
    channel = payload[2]
    body = payload[4:]
    return {"kind": "first" if (t & 0x02) else "single", "seq": seq, "channel": channel, "body": body}


# --------------------------------------------------------------------------
# Message payload builder ([HDR][params])
# --------------------------------------------------------------------------
class Payload:
    def __init__(self, msg_id: int, variable: bool, response: bool, count: bool):
        h = msg_id & 0x3F
        if variable:
            h |= 0x80
        if response:
            h |= 0x40
        self._header = h
        self._variable = variable
        self._count = count
        self._n = 0
        self._buf = bytearray()

    @staticmethod
    def fixed(msg_id: int) -> "Payload":
        return Payload(msg_id, False, False, False)

    @staticmethod
    def variable_no_count(msg_id: int) -> "Payload":
        return Payload(msg_id, True, False, False)

    @staticmethod
    def variable(msg_id: int) -> "Payload":
        """Variable format, request, WITH a leading param-count byte."""
        return Payload(msg_id, True, False, True)

    @staticmethod
    def variable_response(msg_id: int) -> "Payload":
        """Variable format, response bit set, WITH a leading param-count byte."""
        return Payload(msg_id, True, True, True)

    def raw(self, b: int) -> "Payload":
        self._buf.append(b & 0xFF)
        return self

    def raw_le16(self, v: int) -> "Payload":
        self._buf += bytes([v & 0xFF, (v >> 8) & 0xFF])
        return self

    def add_byte(self, pid: int, v: int) -> "Payload":
        self._buf += bytes([pid & 0xFF, v & 0xFF]); self._n += 1
        return self

    def add_short(self, pid: int, v: int) -> "Payload":
        self._buf += bytes([pid & 0xFF, v & 0xFF, (v >> 8) & 0xFF]); self._n += 1
        return self

    def add_int32(self, pid: int, v: int) -> "Payload":
        self._buf += bytes([pid & 0xFF]) + (v & 0xFFFFFFFF).to_bytes(4, "little")
        self._n += 1
        return self

    def add_long(self, pid: int, v: int) -> "Payload":
        self._buf += bytes([pid & 0xFF]) + (v & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        self._n += 1
        return self

    def add_string16(self, pid: int, s: str) -> "Payload":
        """String with a 2-byte little-endian length prefix."""
        u = s.encode("utf-8")
        self._buf += bytes([pid & 0xFF]) + len(u).to_bytes(2, "little") + u
        self._n += 1
        return self

    def add_string(self, pid: int, s: str) -> "Payload":
        u = s.encode("utf-8")
        self._buf += bytes([pid & 0xFF, len(u) & 0xFF]) + u; self._n += 1
        return self

    def build(self) -> bytes:
        out = bytearray([self._header])
        if self._variable and self._count:
            out.append(self._n & 0xFF)
        out += self._buf
        return bytes(out)


# --------------------------------------------------------------------------
# Channels and specific messages we use
# --------------------------------------------------------------------------
CH_CONTROL = 0x01
CH_WEATHER = 0x05
CH_NOTIFICATION = 0x07
CH_MEDIA = 0x09
CH_SETTINGS = 0x0B
CH_QUICK_REPLY = 0x12   # Samsung's "QuickMessageService" (serviceId 18 == channel)

# --- notifications (channel 0x07) ---
MSG_NOTI_POPUP = 0x00       # normal popup alert
MSG_NOTI_NO_POPUP = 0x05    # silent, list only
MSG_NOTI_DND = 0x12         # surfaced despite Do Not Disturb
MSG_NOTI_ICON = 0x03        # band -> phone: "send me this icon"; phone -> band: the pixels
MSG_NOTI_CLEAR = 0x02       # band -> phone: user dismissed a notification
MSG_NOTI_ACTION = 0x0A      # band -> phone: user picked a quick-reply button
                             # (Samsung: NotiPacketConstants.CommandType.NOTIFICATION_ACTION)

# ALERT_TYPE (param 0x0F) values, from Samsung's NotiDBConstants.AlertType. This
# is what makes the band buzz - it's independent of popup/no-popup (the MSG_ID).
# The reference hardcodes 1 (VIBRATION_ONLY) for everything without saying so.
ALERT_SILENT = 0
ALERT_VIBRATION_ONLY = 1
ALERT_SOUND_ONLY = 2
ALERT_SOUND_AND_VIBRATION = 3
MSG_LARGE_DATA_ACK = 0x3E   # band -> phone: ack for a whole big-data transfer

# Big-data chunking, used when a ch 0x07 message is too big for one SAP message:
#   [0x3f][(version<<3)|flag][idx:4 LE][ data, up to cap-6 ]
#   flag: 0 = start, 1 = middle, 2 = end;  version is a rolling 0-31 id
BIGDATA_MARKER = 0x3F
BIGDATA_CAP = 980           # SA framework's per-peer max message size

MSG_INIT_SETTING = 0x03
MSG_LICENSE = 0x04
MSG_BATTERY = 0x02

# --- media (channel 0x09) ---
# Request ids (band -> phone), from Samsung's own SMediaPacketInfo:
#   1 MEDIACHANGED  2 METADATA  3 PLAYBACKSTATE  4 REMOTE_CONTROL  5 CAPABILITY
#   6 VOLUME_REQ    7 VOLUME_CONTROL  8 APP_INFO  9 QUEUE  16 SKIP_TO_ITEM
# Phone -> band pushes use the request id + 15 (confirmed: METADATA 2->17,
# PLAYBACKSTATE 3->18, CAPABILITY 5->20, VOLUME 6->21).
MSG_MEDIA_STATE_REQ_A = 0x01    # MEDIACHANGED
MSG_MEDIA_METADATA_REQ = 0x02
MSG_MEDIA_PLAYBACK_REQ = 0x03
MSG_MEDIA_KEY = 0x04            # REMOTE_CONTROL: transport button (press AND release)
MSG_MEDIA_CAPABILITY_REQ = 0x05
MSG_MEDIA_VOLUME_REQ = 0x06     # "what's your volume?"
MSG_MEDIA_VOLUME = 0x07         # VOLUME_CONTROL: set/up/down/mute
MSG_MEDIA_STATE_REQ_B = 0x08    # APP_INFO
MSG_NOW_PLAYING = 0x11          # phone -> band: track metadata (17)
MSG_PLAYBACK_STATE = 0x12       # phone -> band: play state / position (18)
MSG_MEDIA_CAPABILITY = 20       # phone -> band: maxVolume / warningVolume
MSG_MEDIA_VOLUME_PUSH = 21      # phone -> band: current volume

KEY_STOP, KEY_PLAYPAUSE, KEY_PREVIOUS, KEY_NEXT = 1, 2, 3, 4
ACTION_RELEASED, ACTION_PRESSED = 1, 2

# Volume commands (ch 0x09 msg 7). Only CMD_SET carries a meaningful value.
VOL_SET, VOL_UP, VOL_DOWN, VOL_MUTE_ON, VOL_MUTE_OFF = 1, 2, 3, 4, 5
VOL_CMD_NAMES = {VOL_SET: "set", VOL_UP: "up", VOL_DOWN: "down",
                 VOL_MUTE_ON: "mute on", VOL_MUTE_OFF: "mute off"}

MEDIA_KEY_NAMES = {
    KEY_STOP: "stop", KEY_PLAYPAUSE: "play/pause",
    KEY_PREVIOUS: "previous", KEY_NEXT: "next",
}

STATE_PLAYING = 3
STATE_PAUSED = 2


def build_battery_request() -> bytes:
    return Payload.fixed(MSG_BATTERY).build()          # -> b"\x02"


def build_license_request(accepted: bool = True) -> bytes:
    return Payload.variable_no_count(MSG_LICENSE).add_byte(0x01, 1 if accepted else 0).build()  # 84 01 01


def build_init_setting(locale_id: int = 100, roaming: bool = False, is24h: bool = False) -> bytes:
    epoch = int(time.time())
    tz = -time.timezone if (time.localtime().tm_isdst == 0) else -time.altzone
    negative = tz < 0
    abs_off = abs(tz)
    return (Payload.variable_no_count(MSG_INIT_SETTING)
            .add_short(0x01, locale_id)
            .add_byte(0x02, 1 if roaming else 0)
            .add_string(0x03, str(epoch))
            .add_byte(0x04, 1 if negative else 0)
            .raw_le16(abs_off)
            .add_byte(0x05, 1 if is24h else 0)
            .build())


# --- weather (channel 0x05) ---
MSG_WEATHER_INFO = 0x01
MSG_WEATHER_ADD_LOCATION = 0x00


def _band_epoch(utc_seconds: int) -> int:
    """The band wants LOCAL time as an epoch, i.e. UTC epoch + tz offset."""
    off = -time.timezone if time.localtime().tm_isdst == 0 else -time.altzone
    return int(utc_seconds) + off


def build_weather(w: dict, fahrenheit: bool = False, uv_text: str = "Low") -> bytes:
    """Weather push (ch 0x05, MSG_ID 1).

    Weather is the odd one out: the header is followed by a 2-byte BIG-endian
    param count (everything else here is little-endian), and fixed-width numeric
    params carry no length byte - only strings do.
    """
    out = bytearray()
    n = 0

    def wb(pid, v):
        nonlocal n
        out.extend([pid & 0xFF, int(v) & 0xFF]); n += 1

    def w16(pid, v):
        nonlocal n
        out.append(pid & 0xFF); out.extend((int(v) & 0xFFFF).to_bytes(2, "little")); n += 1

    def w32(pid, v):
        nonlocal n
        out.append(pid & 0xFF); out.extend((int(v) & 0xFFFFFFFF).to_bytes(4, "little")); n += 1

    def ws(pid, s):
        nonlocal n
        u = (s or "").encode("utf-8")
        out.append(pid & 0xFF); out.append(len(u) & 0xFF); out.extend(u); n += 1

    # settings block (the phone's own weather-app settings)
    wb(0x08, 1)                       # cp type = ACC
    wb(0x06, 1 if fahrenheit else 0)  # temp scale (celsius = 0)
    wb(0x09, 0)                       # show "use location" popup
    wb(0x0A, 1)                       # location on
    wb(0x07, 0)                       # auto-refresh duration
    wb(0x0B, 1)                       # has current location

    # current conditions
    wb(0x0C, 0)                       # location index
    wb(0x12, 1)                       # is current location
    ws(0x0D, w["city"]); ws(0x0E, w["region"]); ws(0x0F, w["country"])
    wb(0x10, w["icon"])
    w32(0x13, _band_epoch(w["time"]))
    w16(0x14, w["temp"]); w16(0x15, w["high"]); w16(0x16, w["low"])
    wb(0x11, w["is_day"])
    w16(0x18, w["high"]); w16(0x19, w["low"])   # no yesterday data: echo today
    w32(0x1A, _band_epoch(w["sunrise"])); w32(0x1B, _band_epoch(w["sunset"]))
    ws(0x1C, "")                      # timezone name
    w16(0x17, w["feels_like"])

    # hourly forecast
    wb(0x1D, len(w["hourly"]))
    for i, h in enumerate(w["hourly"]):
        wb(0x1E, i); wb(0x22, h["is_day"]); w32(0x1F, _band_epoch(h["t"]))
        wb(0x20, h["icon"]); w16(0x21, h["temp"]); wb(0x23, h["precip"])

    # daily forecast
    wb(0x24, len(w["daily"]))
    for i, d in enumerate(w["daily"]):
        wb(0x25, i); w32(0x26, _band_epoch(d["t"]))
        wb(0x27, d["icon"]); wb(0x28, d["icon"]); wb(0x29, d["icon"])
        w16(0x2A, d["max"]); w16(0x2B, d["min"])

    # air quality / UV / wind / humidity (no AQI source here -> zeros)
    w16(0x2C, 0); w16(0x2D, 0); w16(0x2E, 0); w16(0x2F, 0); w16(0x30, 0); w16(0x31, 0)
    w16(0x32, w["uv"]); ws(0x33, uv_text)
    w16(0x34, w["wind_speed"]); ws(0x35, w["wind_dir"])
    w16(0x36, w["humidity"])

    # header: variable + response + MSG_ID 1, then the 2-byte BE param count
    return bytes([0xC1, (n >> 8) & 0xFF, n & 0xFF]) + bytes(out)


def java_string_hash(s: str) -> int:
    """Java's String.hashCode(), needed to reproduce the band's APP_ID."""
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    if h >= 2 ** 31:
        h -= 2 ** 32
    return h


def app_id_for(pkg: str) -> str:
    """Stand-in APP_ID the band uses to key an app (and later ask for its icon)."""
    return str(abs(java_string_hash(pkg)))


def build_notification(seq_id: int, title: str, body: str = "", app_name: str = "",
                       pkg: str = "", when_ms: int = None, category: str = "",
                       icon_newly_added: bool = False, popup: bool = True,
                       need_quick_reply: bool = False,
                       alert_type: int = ALERT_VIBRATION_ONLY,
                       is_urgent: bool = True) -> bytes:
    """Push a notification to the band (ch 0x07).

    Param ORDER matters - the band reads these positionally against a per-MSG_ID
    shape, so this follows the confirmed order rather than sorting by id.
    """
    if when_ms is None:
        when_ms = int(time.time() * 1000)
    heading = title or app_name or "Notification"
    msg_id = MSG_NOTI_POPUP if popup else MSG_NOTI_NO_POPUP

    b = (Payload.variable(msg_id)
         .add_byte(0x00, 1)                       # NOTI_TYPE
         .add_int32(0x01, seq_id)                 # SEQUENCE_ID
         .add_string(0x06, app_id_for(pkg))       # APP_ID
         .add_long(0x02, when_ms)                 # TIME
         .add_string(0x03, heading))              # NOTI_HEADING
    if body:
        b.add_string16(0x04, body)                # NOTI_BODY (2-byte len)
    if category:
        b.add_string(0x0C, category)
    if need_quick_reply:
        b.add_byte(0x0D, 1)                       # NEED_QUICK_RESPONSE - shows the
                                                    # band's reply-button picker
    if app_name:
        b.add_string(0x0E, app_name)              # APP_NAME
    b.add_byte(0x0F, alert_type)                  # ALERT_TYPE (0=silent, 1=vibrate)
    b.add_byte(0x10, 1 if is_urgent else 0)       # IS_URGENT
    if pkg:
        # IMAGE_URL is what makes the band bother asking for an icon at all.
        b.add_string(0x09, "APP_ICON_" + app_id_for(pkg))
        if icon_newly_added:
            b.add_byte(0x13, 1)
        b.add_string(0x12, pkg)                   # APP_PACKAGE_NAME
    return b.build()


def parse_icon_request(body: bytes):
    """Band's icon request (ch 0x07 msg 3, fixed+request): 03 [09 len URL][23 size].

    Returns dict(url, size) or None.
    """
    if not body or (body[0] & 0x40) != 0 or (body[0] & 0x3F) != MSG_NOTI_ICON:
        return None
    off, url, size = 1, None, None
    while off < len(body):
        pid = body[off]
        if pid == 0x09 and off + 1 < len(body):
            ln = body[off + 1]
            if off + 2 + ln > len(body):
                break
            url = body[off + 2:off + 2 + ln].decode("utf-8", "replace")
            off += 2 + ln
        elif pid == 0x23 and off + 1 < len(body):
            size = body[off + 1]
            off += 2
        else:
            break   # unknown/short param - stop clean
    if url is None or size is None:
        return None
    return {"url": url, "size": size}


def parse_notification_clear(body: bytes):
    """Band dismissed a notification (ch 0x07 msg 2, fixed+request).

    Observed: 02 | 01 <seq:4 LE>   -> param 0x01 is the SEQUENCE_ID we sent.
    Returns the sequence id, or None.
    """
    if not body or (body[0] & 0x3F) != MSG_NOTI_CLEAR or (body[0] & 0x40) != 0:
        return None
    off = 1
    while off + 1 < len(body):
        pid = body[off]
        if pid == 0x01 and off + 4 < len(body):
            return int.from_bytes(body[off + 1:off + 5], "little")
        off += 2   # unknown 1-byte param; skip id+value
    return None


def parse_notification_action(body: bytes):
    """Band picked a quick-reply button (ch 0x07 msg 10, VARIABLE format).

    From Samsung's own NotiPacketParser.parsePacketToNotiUnit/parseParamValue:
      byte[0] = header, must have the format bit (0x80) set
      byte[1] = param count
      then that many [paramId][value] entries:
        1  SEQUENCE_ID    int32 LE (4 bytes, no length prefix)
        10 REPLY_MESSAGE  [len:1][utf8]   <- the actual button text
        11 RESULT         1 byte
        35 ICON_SIZE      1 byte
        6  APP_ID         [len:1][ascii digits]
        9  IMAGE_URL      [len:1][utf8]

    Returns dict(seq, text) - `text` is the exact label of the button the user
    tapped, so no index bookkeeping is needed. None if this isn't msg 10 or the
    body is too short to hold a count byte.
    """
    if len(body) < 2 or (body[0] & 0x3F) != MSG_NOTI_ACTION or (body[0] & 0x80) == 0:
        return None
    count = body[1]
    off = 2
    seq = None
    text = None
    for _ in range(count):
        if off >= len(body):
            break
        pid = body[off]
        off += 1
        if pid == 0x01:                      # SEQUENCE_ID: fixed int32 LE
            if off + 4 > len(body):
                break
            seq = int.from_bytes(body[off:off + 4], "little")
            off += 4
        elif pid == 0x0B:                    # RESULT: fixed 1 byte
            off += 1
        elif pid == 0x23:                    # NOTI_REQUIRED_ICON_SIZE: fixed 1 byte
            off += 1
        elif pid in (0x06, 0x09, 0x0A):       # APP_ID / IMAGE_URL / REPLY_MESSAGE: [len:1][data]
            if off >= len(body):
                break
            ln = body[off]
            off += 1
            val = body[off:off + ln].decode("utf-8", "replace")
            off += ln
            if pid == 0x0A:
                text = val
        else:
            break   # unknown param id - stop clean rather than misreading a value
    if seq is None and text is None:
        return None
    return {"seq": seq, "text": text}


def build_quick_reply_list(items) -> bytes:
    """Set the band's quick-reply button list (ch 0x12, msg 0, FIXED format).

    Ported from Samsung's SAMessageDataPacketConstructor.constructQuickMessage
    PacketSetInfo: header has no param-count byte (NO_OF_PARAM never set), just
    a raw count value as param 0, followed by one [1][len][utf8] per item.

        00 | 00 <count>  | 01 <len> <text> | 01 <len> <text> | ...
    """
    out = bytearray([0x00, 0x00, len(items) & 0xFF])
    for text in items:
        u = text.encode("utf-8")
        out += bytes([0x01, len(u) & 0xFF]) + u
    return bytes(out)


def build_quick_reply_switch(enabled: bool) -> bytes:
    """Turn the band's quick-reply feature on/off (ch 0x12, msg 2, FIXED format).

    Ported from constructQuickMessageSwitchStatusPacketInfo; verified byte-exact
    against a captured on-the-wire value from the Gadgetbridge author (02 03 01).
    """
    return bytes([0x02, 0x03, 1 if enabled else 0])


def build_icon_response(url: str, size: int, rgba: bytes) -> bytes:
    """43 [09 len URL][07 <4B LE len> PIXELS][23 <size 4B>].

    `rgba` is size*size pixels, 4 bytes each, [R][G][B][255-alpha].
    """
    out = bytearray()
    out.append(0x40 | MSG_NOTI_ICON)          # fixed, response -> 0x43
    u = url.encode("utf-8")
    out.append(0x09); out.append(len(u) & 0xFF); out += u
    out.append(0x07); out += len(rgba).to_bytes(4, "little"); out += rgba
    out.append(0x23); out += bytes([size & 0xFF, 0, 0, 0])
    return bytes(out)


class BigData:
    """Chunker for oversized ch 0x07 messages (rolling version id per transfer)."""

    def __init__(self):
        self._version = 0

    def chunk(self, data: bytes, cap: int = BIGDATA_CAP):
        # Small enough? The source only engages chunking for oversized messages.
        if len(data) <= cap:
            return [data]
        ver = self._version
        self._version = (self._version + 1) % 32
        data_cap = cap - 6
        chunks, off, idx = [], 0, 0
        while off < len(data):
            end = min(off + data_cap, len(data))
            flag = 0 if idx == 0 else (2 if end == len(data) else 1)
            head = bytes([BIGDATA_MARKER, ((ver << 3) | flag) & 0xFF]) + idx.to_bytes(4, "little")
            chunks.append(head + data[off:end])
            off, idx = end, idx + 1
        return chunks


def parse_bigdata_ack(body: bytes):
    """0x3e ack: [hdr][byte] -> top 5 bits version, bottom 2 bits success code."""
    if len(body) < 2:
        return None
    b = body[1]
    return {"version": (b & 0xF8) >> 3, "success": (b & 0x03) != 0}


def build_now_playing(title: str, artist: str, duration_ms: int = 0,
                      album_art: str = "", app_id: str = "") -> bytes:
    """Track metadata push (ch 0x09, MSG_ID 17)."""
    return (Payload.variable_response(MSG_NOW_PLAYING)
            .add_long(0x01, duration_ms)
            .add_string(0x02, title)
            .add_string(0x03, artist)
            .add_string(0x04, album_art)
            .add_string(0x05, app_id)
            .build())


def build_playback_state(playing: bool, position_ms: int = 0,
                         queue_item_id: int = 0, app_id: str = "") -> bytes:
    """Playback state push (ch 0x09, MSG_ID 18).

    Param order is 1, 2, 4, 3 - the band reads this positionally, so the
    out-of-order id 4 before id 3 is deliberate, not a mistake.
    """
    return (Payload.variable_response(MSG_PLAYBACK_STATE)
            .add_byte(0x01, STATE_PLAYING if playing else STATE_PAUSED)
            .add_long(0x02, position_ms)
            .add_long(0x04, queue_item_id)
            .add_string(0x03, app_id)
            .build())


def parse_media_button(body: bytes):
    """Return dict(key_code, key_action) for a ch 0x09 MSG_ID 4 event, else None.

    A single physical tap produces TWO of these: one with key_action=ACTION_PRESSED
    and one with ACTION_RELEASED. Act on ACTION_PRESSED only.
    """
    if len(body) < 2:
        return None
    if (body[0] & 0x3F) != MSG_MEDIA_KEY:
        return None
    variable = (body[0] >> 7) & 1
    i = 2 if variable else 1        # skip the param-count byte when present
    key_code = key_action = 0
    while i + 1 < len(body):
        pid, val = body[i], body[i + 1]
        i += 2
        if pid == 0x01:
            key_code = val
        elif pid == 0x02:
            key_action = val
        else:
            break
    return {"key_code": key_code, "key_action": key_action}


def build_media_volume(volume: int, headset_connected: bool = False) -> bytes:
    """Report the PC's current volume to the band (ch 0x09, msg 21).

    Built exactly as Samsung's SMediaVolumeData.toMessageData() does:
      header = format 0 (fixed) | type 1 (response) | id 21   -> 0x55
      then a param-count byte (written whenever params > 0, independent of the
      format bit), then [01][volume int32 LE][02][headset: 2=yes 1=no].
    """
    return (bytes([0x40 | MSG_MEDIA_VOLUME_PUSH, 2, 0x01])
            + (int(volume) & 0xFFFFFFFF).to_bytes(4, "little")
            + bytes([0x02, 2 if headset_connected else 1]))


def build_media_capability(max_volume: int = 100, warning_volume: int = 100) -> bytes:
    """Tell the band our volume range (ch 0x09, msg 20).

    This is what makes the band's slider match the PC: it scales to maxVolume.
    Mirrors SMediaCapabilityFeatureData.toMessageData(); both params are int32 LE.
    """
    return (bytes([0x40 | MSG_MEDIA_CAPABILITY, 2, 0x01])
            + (int(max_volume) & 0xFFFFFFFF).to_bytes(4, "little")
            + bytes([0x02])
            + (int(warning_volume) & 0xFFFFFFFF).to_bytes(4, "little"))


def parse_media_volume(body: bytes):
    """Volume command from the band (ch 0x09 MSG_ID 7).

      xx 02 | 01 <command> | 02 <value>
    Both params are always on the wire even when the value is meaningless.
    Returns dict(command, value) or None.
    """
    if len(body) < 2 or (body[0] & 0x3F) != MSG_MEDIA_VOLUME:
        return None
    variable = (body[0] >> 7) & 1
    i = 2 if variable else 1        # skip the param-count byte when present
    command = value = 0
    while i + 1 < len(body):
        pid, val = body[i], body[i + 1]
        i += 2
        if pid == 0x01:
            command = val
        elif pid == 0x02:
            value = val
        else:
            break
    return {"command": command, "value": value}


def parse_battery_response(body: bytes):
    """Return (level, charging) if body is the battery reply (ch 0x0B, msg 2, response), else None."""
    if len(body) < 1:
        return None
    msg_id = body[0] & 0x3F
    is_response = (body[0] >> 6) & 1
    is_variable = (body[0] >> 7) & 1
    if msg_id != MSG_BATTERY or is_variable or not is_response:
        return None
    level, charging = -1, False
    i = 1
    while i + 1 <= len(body) - 1 + 1 and i + 1 <= len(body):
        pid = body[i]; val = body[i + 1]; i += 2
        if pid == 0x05:
            level = val
        elif pid == 0x06:
            charging = val != 0
    return level, charging
