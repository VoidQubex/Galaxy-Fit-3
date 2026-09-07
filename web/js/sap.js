/*
 * sap.js — Samsung Accessory Protocol for the Galaxy Fit 3, ported to browser JS
 * from sap.py / bandface.py / weather.py in this repo (AGPL-3.0 via Gadgetbridge).
 *
 * Pure bytes-in / bytes-out — no Bluetooth here. Layers:
 *   transport  frame  [LEN:2 BE][HDR_CRC:2 BE][PAYLOAD][DATA_CRC:2 BE]  CRC-16/ARC
 *   handshake  hello  raw 0x68.. frames (NOT CRC-wrapped)
 *   session    packet [TYPE][SEQ][CHAN][CHAN][BODY]  + ACKs
 *   message    [HDR][params]
 *
 * Everything hangs off the global `SAP` namespace (classic scripts, no modules, so
 * the page also works from a plain file:// if you only want to eyeball the UI).
 */
"use strict";

(function () {
  // --------------------------------------------------------------------------
  // CRC-16/ARC (a.k.a. CRC-16/ANSI / IBM): poly 0x8005 reflected -> 0xA001, init 0.
  // --------------------------------------------------------------------------
  function crc16(data, init) {
    init = init || 0;
    let crc = init;
    for (let i = 0; i < data.length; i++) {
      crc ^= data[i];
      for (let b = 0; b < 8; b++) {
        crc = crc & 1 ? (crc >>> 1) ^ 0xA001 : crc >>> 1;
      }
    }
    return crc & 0xFFFF;
  }

  function toBytes(buf) {
    return buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  }
  function be16(v) { return [(v >> 8) & 0xFF, v & 0xFF]; }
  function le16(v) { return [v & 0xFF, (v >> 8) & 0xFF]; }
  function le32(v) { return [v & 0xFF, (v >>> 8) & 0xFF, (v >>> 16) & 0xFF, (v >>> 24) & 0xFF]; }
  // 64-bit little endian from a non-negative safe integer.
  function le64(v) {
    let lo = v % 4294967296;
    let hi = Math.floor(v / 4294967296);
    return [(lo & 0xFF), (lo >>> 8) & 0xFF, (lo >>> 16) & 0xFF, (lo >>> 24) & 0xFF,
            (hi & 0xFF), (hi >>> 8) & 0xFF, (hi >>> 16) & 0xFF, (hi >>> 24) & 0xFF];
  }
  function readU16BE(a, off) { return (a[off] << 8) | a[off + 1]; }
  function readU32LE(a, off) { return (a[off]) | (a[off + 1] << 8) | (a[off + 2] << 16) | (a[off + 3] << 24); }
  function concat(list) {
    let n = 0;
    for (const b of list) n += b.length;
    const out = new Uint8Array(n);
    let o = 0;
    for (const b of list) { out.set(b, o); o += b.length; }
    return out;
  }
  function hex(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0");
    return s;
  }
  function hexSpace(bytes, max) {
    let parts = [];
    const n = max && max > 0 ? Math.min(max, bytes.length) : bytes.length;
    for (let i = 0; i < n; i++) parts.push(bytes[i].toString(16).padStart(2, "0"));
    return parts.join(" ");
  }

  // --------------------------------------------------------------------------
  // Transport frame
  // --------------------------------------------------------------------------
  function frameWrap(payload) {
    payload = toBytes(payload);
    const len = payload.length;
    const lenb = new Uint8Array(be16(len));
    const hdrCrc = crc16(lenb);
    const dataCrc = crc16(payload);
    const out = new Uint8Array(6 + len);
    out.set(lenb, 0);
    out.set(be16(hdrCrc), 2);
    out.set(payload, 4);
    out.set(be16(dataCrc), 4 + len);
    return out;
  }

  function frameIsHello(frame) {
    return frame.length >= 1 && frame[0] === 0x68;
  }
  function helloIsBand(frame) {
    return frameIsHello(frame) && frame.length >= 2 && frame[1] === 0x14;
  }

  // Returns payload bytes or null if malformed / CRC mismatch.
  function frameUnwrap(frame) {
    frame = toBytes(frame);
    if (frame.length < 6) return null;
    const ln = readU16BE(frame, 0);
    if (frame.length !== ln + 6) return null;
    if (crc16(frame.subarray(0, 2)) !== readU16BE(frame, 2)) return null;
    if (crc16(frame.subarray(4, 4 + ln)) !== readU16BE(frame, 4 + ln)) return null;
    return frame.subarray(4, 4 + ln);
  }

  // --------------------------------------------------------------------------
  // HELLO handshake (fixed 105-byte layout, sent raw / plaintext)
  // --------------------------------------------------------------------------
  const PHONE_HEADER = [0x68, 0x15, 0x04, 0x01, 0x00, 0xFF, 0x03];
  const HELLO_LEN = 105;
  const PARAM_OFF = 43, PARAM_LEN = 7;
  const MODEL_OFF = 50, VENDOR_OFF = 66, VENDOR2_OFF = 82, FIELD_LEN = 16;
  const ADDR_OFF = 98, ADDR_LEN = 6, TRAILER_OFF = 104;

  function getField(f, off) {
    let end = off;
    const limit = off + FIELD_LEN;
    while (end < limit && f[end] !== 0) end++;
    // ASCII-ish decode
    let s = "";
    for (let i = off; i < end; i++) s += String.fromCharCode(f[i]);
    return s;
  }

  function parseBandHello(frame) {
    if (!helloIsBand(frame) || frame.length < HELLO_LEN) return null;
    return {
      param_block: frame.subarray(PARAM_OFF, PARAM_OFF + PARAM_LEN),
      address: frame.subarray(ADDR_OFF, ADDR_OFF + ADDR_LEN),
      model: getField(frame, MODEL_OFF),
      vendor: getField(frame, VENDOR_OFF),
    };
  }

  function randomToken() {
    // 32 upper-case hex chars (a session id, not a security key).
    const a = new Uint8Array(16);
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      crypto.getRandomValues(a);
    } else {
      for (let i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256);
    }
    let s = "";
    for (let i = 0; i < a.length; i++) s += a[i].toString(16).padStart(2, "0");
    return s.toUpperCase();
  }

  function putField(f, off, value) {
    const s = String(value || "");
    // Keep a NUL terminator inside the field.
    const n = Math.min(s.length, FIELD_LEN - 1);
    for (let i = 0; i < n; i++) f[off + i] = s.charCodeAt(i) & 0xFF;
  }

  function buildPhoneHello(bandHello, token32hex, phoneModel, phoneVendor, deviceAddress) {
    const f = new Uint8Array(HELLO_LEN);
    f.set(PHONE_HEADER, 0);
    const sap = "SAP_" + token32hex;
    for (let i = 0; i < Math.min(sap.length, 36); i++) f[7 + i] = sap.charCodeAt(i);
    f.set(bandHello.param_block, PARAM_OFF);
    putField(f, MODEL_OFF, phoneModel);
    putField(f, VENDOR_OFF, phoneVendor);
    putField(f, VENDOR2_OFF, phoneVendor);
    if (deviceAddress && deviceAddress.length === ADDR_LEN) f.set(deviceAddress, ADDR_OFF);
    f[TRAILER_OFF] = 0x02;
    return f;
  }

  // --------------------------------------------------------------------------
  // Session packets
  // --------------------------------------------------------------------------
  const TYPE_SINGLE = 0x01, TYPE_FIRST = 0x03, TYPE_CONT = 0x07, TYPE_ACK = 0x88;
  const ACK_FLAG_SINGLE = 0x80, ACK_FLAG_CHUNK = 0xC0;

  function packetEncode(channel, type, seq, body) {
    const head = 4;                       // [TYPE][SEQ][CHAN][CHAN]
    const out = new Uint8Array(head + body.length);
    out[0] = type; out[1] = seq & 0xFF;
    out[2] = channel & 0xFF; out[3] = channel & 0xFF;
    out.set(body, head);
    return out;
  }
  function packetEncodeSingle(channel, seq, body) {
    return packetEncode(channel, TYPE_SINGLE, seq, toBytes(body));
  }
  function packetEncodeFirstFragment(channel, seq, chunk) {
    return packetEncode(channel, TYPE_FIRST, seq, toBytes(chunk));
  }
  function packetEncodeContinuation(seq, chunk) {
    const out = new Uint8Array(2 + chunk.length);
    out[0] = TYPE_CONT; out[1] = seq & 0xFF;
    out.set(chunk, 2);
    return out;
  }
  function packetEncodeAck(seq, chunk) {
    return new Uint8Array([TYPE_ACK, seq & 0xFF, chunk ? ACK_FLAG_CHUNK : ACK_FLAG_SINGLE, 0x00]);
  }

  function buildFrames(channel, seq, body, mtu) {
    // Frames to send `body` on `channel`, fragmenting if it exceeds the MTU.
    const maxFrame = mtu - 3;
    const single = frameWrap(packetEncodeSingle(channel, seq, body));
    if (single.length <= maxFrame) return [single];

    const firstCap = maxFrame - 6 - 4;
    const contCap = maxFrame - 6 - 2;
    if (firstCap <= 0 || contCap <= 0) throw new Error("MTU too small to fragment (" + mtu + ")");

    const frames = [];
    let off = Math.min(firstCap, body.length);
    frames.push(frameWrap(packetEncodeFirstFragment(channel, seq, body.subarray(0, off))));
    let contSeq = (seq | 0x01);
    while (off < body.length) {
      const end = Math.min(off + contCap, body.length);
      frames.push(frameWrap(packetEncodeContinuation(contSeq, body.subarray(off, end))));
      off = end;
    }
    return frames;
  }

  function packetParse(payload) {
    if (!payload || payload.length < 2) return { kind: "unknown", seq: -1, channel: -1, body: new Uint8Array(0) };
    const t = payload[0], seq = payload[1];
    if (t === TYPE_ACK) return { kind: "ack", seq, channel: -1, body: new Uint8Array(0) };
    if (t & 0x04) return { kind: "cont", seq, channel: -1, body: payload.subarray(2) };
    if (payload.length < 4) return { kind: "unknown", seq, channel: -1, body: new Uint8Array(0) };
    const channel = payload[2];
    const body = payload.subarray(4);
    return { kind: t & 0x02 ? "first" : "single", seq, channel, body };
  }

  // --------------------------------------------------------------------------
  // Message payload builder ([HDR][params])
  // --------------------------------------------------------------------------
  class Payload {
    constructor(msgId, variable, response, count) {
      let h = msgId & 0x3F;
      if (variable) h |= 0x80;
      if (response) h |= 0x40;
      this.header = h;
      this.variable = variable;
      this.count = count;
      this.n = 0;
      this.buf = [];
    }
    static fixed(msgId) { return new Payload(msgId, false, false, false); }
    static variableNoCount(msgId) { return new Payload(msgId, true, false, false); }
    static variable(msgId) { return new Payload(msgId, true, false, true); }
    static variableResponse(msgId) { return new Payload(msgId, true, true, true); }

    raw(b) { this.buf.push(b & 0xFF); return this; }
    rawLE16(v) { this.buf.push(v & 0xFF, (v >> 8) & 0xFF); return this; }
    addByte(pid, v) { this.buf.push(pid & 0xFF, v & 0xFF); this.n++; return this; }
    addShort(pid, v) { this.buf.push(pid & 0xFF, v & 0xFF, (v >> 8) & 0xFF); this.n++; return this; }
    addInt32(pid, v) { this.buf.push(pid & 0xFF); this.buf.push.apply(this.buf, le32(v & 0xFFFFFFFF)); this.n++; return this; }
    addLong(pid, v) { this.buf.push(pid & 0xFF); this.buf.push.apply(this.buf, le64(v)); this.n++; return this; }
    addString16(pid, s) {
      const u = utf8(String(s || ""));
      this.buf.push(pid & 0xFF, u.length & 0xFF, (u.length >> 8) & 0xFF);
      for (let i = 0; i < u.length; i++) this.buf.push(u[i]);
      this.n++;
      return this;
    }
    addString(pid, s) {
      const u = utf8(String(s || ""));
      this.buf.push(pid & 0xFF, u.length & 0xFF);
      for (let i = 0; i < u.length; i++) this.buf.push(u[i]);
      this.n++;
      return this;
    }
    build() {
      const out = [this.header];
      if (this.variable && this.count) out.push(this.n & 0xFF);
      return new Uint8Array(out.concat(this.buf));
    }
  }

  function utf8(s) {
    // TextEncoder gives a Uint8Array (1 byte code units for BMP mostly; handles surrogates)
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(s);
    return new Uint8Array([].map.call(unescape(encodeURIComponent(s)), c => c.charCodeAt(0)));
  }
  function decodeUtf8(u) {
    if (typeof TextDecoder !== "undefined") return new TextDecoder("utf-8").decode(u);
    let s = "";
    for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
    return decodeURIComponent(escape(s));
  }

  // --------------------------------------------------------------------------
  // Channels & messages
  // --------------------------------------------------------------------------
  const CH_CONTROL = 0x01, CH_WEATHER = 0x05, CH_NOTIFICATION = 0x07,
        CH_MEDIA = 0x09, CH_SETTINGS = 0x0B, CH_BANDFACE = 0x06, CH_QUICK_REPLY = 0x12;

  const MSG_NOTI_POPUP = 0x00, MSG_NOTI_NO_POPUP = 0x05, MSG_NOTI_DND = 0x12;
  const MSG_NOTI_ICON = 0x03, MSG_NOTI_CLEAR = 0x02, MSG_NOTI_ACTION = 0x0A;
  const MSG_LARGE_DATA_ACK = 0x3E;

  const ALERT_SILENT = 0, ALERT_VIBRATION_ONLY = 1, ALERT_SOUND_ONLY = 2, ALERT_SOUND_AND_VIBRATION = 3;

  const BIGDATA_MARKER = 0x3F, BIGDATA_CAP = 980;

  const MSG_INIT_SETTING = 0x03, MSG_LICENSE = 0x04, MSG_BATTERY = 0x02;

  // media channel (0x09)
  const MSG_MEDIA_STATE_REQ_A = 0x01, MSG_MEDIA_METADATA_REQ = 0x02, MSG_MEDIA_PLAYBACK_REQ = 0x03;
  const MSG_MEDIA_KEY = 0x04, MSG_MEDIA_CAPABILITY_REQ = 0x05, MSG_MEDIA_VOLUME_REQ = 0x06;
  const MSG_MEDIA_VOLUME = 0x07, MSG_MEDIA_STATE_REQ_B = 0x08;
  const MSG_NOW_PLAYING = 0x11, MSG_PLAYBACK_STATE = 0x12;
  const MSG_MEDIA_CAPABILITY = 20, MSG_MEDIA_VOLUME_PUSH = 21;

  const KEY_STOP = 1, KEY_PLAYPAUSE = 2, KEY_PREVIOUS = 3, KEY_NEXT = 4;
  const ACTION_RELEASED = 1, ACTION_PRESSED = 2;
  const VOL_SET = 1, VOL_UP = 2, VOL_DOWN = 3, VOL_MUTE_ON = 4, VOL_MUTE_OFF = 5;
  const STATE_PLAYING = 3, STATE_PAUSED = 2;
  const MEDIA_KEY_NAMES = { 1: "stop", 2: "play/pause", 3: "previous", 4: "next" };
  const VOL_CMD_NAMES = { 1: "set", 2: "up", 3: "down", 4: "mute on", 5: "mute off" };

  // weather channel (0x05)
  const MSG_WEATHER_INFO = 0x01, MSG_WEATHER_ADD_LOCATION = 0x00;

  // --------------------------------------------------------------------------
  // Small requests
  // --------------------------------------------------------------------------
  function buildBatteryRequest() { return Payload.fixed(MSG_BATTERY).build(); } // -> 02

  function buildLicenseRequest(accepted) {
    return Payload.variableNoCount(MSG_LICENSE).addByte(0x01, accepted === false ? 0 : 1).build();
  }

  // Local timezone offset, seconds EAST of UTC (mirrors server._band time logic).
  function tzSecondsEast() {
    return -(new Date().getTimezoneOffset() * 60);
  }
  function bandEpoch(utcSeconds) { return Math.floor(utcSeconds) + tzSecondsEast(); }
  function nowSeconds() { return Math.floor(Date.now() / 1000); }

  function buildInitSetting(localeId, roaming, is24h) {
    localeId = localeId || 100; roaming = !!roaming; is24h = !!is24h;
    const epoch = nowSeconds();
    let off = tzSecondsEast();
    const negative = off < 0;
    off = Math.abs(off);
    return Payload.variableNoCount(MSG_INIT_SETTING)
      .addShort(0x01, localeId)
      .addByte(0x02, roaming ? 1 : 0)
      .addString(0x03, String(epoch))
      .addByte(0x04, negative ? 1 : 0)
      .rawLE16(off)
      .addByte(0x05, is24h ? 1 : 0)
      .build();
  }

  // --------------------------------------------------------------------------
  // Java String.hashCode() — the band keys apps by this.
  // --------------------------------------------------------------------------
  function javaStringHash(s) {
    let h = 0;
    s = String(s);
    for (let i = 0; i < s.length; i++) {
      h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
    }
    return h;
  }
  function appIdFor(pkg) { return String(Math.abs(javaStringHash(pkg))); }

  // --------------------------------------------------------------------------
  // Notifications (channel 0x07)
  // --------------------------------------------------------------------------
  function buildNotification(opts) {
    opts = opts || {};
    const seqId = opts.seq || 0;
    const title = opts.title || "";
    const body = opts.body || "";
    const appName = opts.app_name || "";
    const pkg = opts.pkg || "";
    let whenMs = opts.when_ms;
    if (whenMs === undefined || whenMs === null) whenMs = Date.now();
    const category = opts.category || "";
    const heading = title || appName || "Notification";
    const msgId = opts.popup === false ? MSG_NOTI_NO_POPUP : MSG_NOTI_POPUP;
    const alertType = opts.alert_type !== undefined ? opts.alert_type : ALERT_VIBRATION_ONLY;

    let b = Payload.variable(msgId)
      .addByte(0x00, 1)                     // NOTI_TYPE
      .addInt32(0x01, seqId)                // SEQUENCE_ID
      .addString(0x06, appIdFor(pkg))       // APP_ID
      .addLong(0x02, whenMs)                // TIME
      .addString(0x03, heading);            // NOTI_HEADING
    if (body) b.addString16(0x04, body);    // NOTI_BODY (2-byte len)
    if (category) b.addString(0x0C, category);
    if (opts.need_quick_reply) b.addByte(0x0D, 1);
    if (appName) b.addString(0x0E, appName);
    b.addByte(0x0F, alertType);
    b.addByte(0x10, opts.is_urgent === false ? 0 : 1);
    if (pkg) {
      // IMAGE_URL is what makes the band bother asking for an icon at all.
      b.addString(0x09, "APP_ICON_" + appIdFor(pkg));
      if (opts.icon_newly_added) b.addByte(0x13, 1);
      b.addString(0x12, pkg);               // APP_PACKAGE_NAME
    }
    return b.build();
  }

  // Band's icon request (ch 0x07 msg 3): 03 [09 len URL][23 size]
  function parseIconRequest(body) {
    if (!body || body.length < 1) return null;
    const hdr = body[0];
    if ((hdr & 0x40) !== 0 || (hdr & 0x3F) !== MSG_NOTI_ICON) return null;
    let off = 1, url = null, size = null;
    while (off < body.length) {
      const pid = body[off];
      if (pid === 0x09 && off + 1 < body.length) {
        const ln = body[off + 1];
        if (off + 2 + ln > body.length) break;
        url = decodeUtf8(body.subarray(off + 2, off + 2 + ln));
        off += 2 + ln;
      } else if (pid === 0x23 && off + 1 < body.length) {
        size = body[off + 1];
        off += 2;
      } else {
        break;
      }
    }
    if (url === null || size === null) return null;
    return { url, size };
  }

  function parseNotificationClear(body) {
    if (!body || body.length < 1) return null;
    const hdr = body[0];
    if ((hdr & 0x3F) !== MSG_NOTI_CLEAR || (hdr & 0x40) !== 0) return null;
    let off = 1;
    while (off + 1 < body.length) {
      const pid = body[off];
      if (pid === 0x01 && off + 4 < body.length) {
        return readU32LE(body, off + 1);
      }
      off += 2;
    }
    return null;
  }

  function parseNotificationAction(body) {
    if (!body || body.length < 2) return null;
    const hdr = body[0];
    if ((hdr & 0x3F) !== MSG_NOTI_ACTION || (hdr & 0x80) === 0) return null;
    const count = body[1];
    let off = 2, seq = null, text = null;
    for (let k = 0; k < count; k++) {
      if (off >= body.length) break;
      const pid = body[off]; off += 1;
      if (pid === 0x01) {
        if (off + 4 > body.length) break;
        seq = readU32LE(body, off); off += 4;
      } else if (pid === 0x0B) {
        off += 1;
      } else if (pid === 0x23) {
        off += 1;
      } else if (pid === 0x06 || pid === 0x09 || pid === 0x0A) {
        if (off >= body.length) break;
        const ln = body[off]; off += 1;
        if (off + ln > body.length) break;
        const val = decodeUtf8(body.subarray(off, off + ln)); off += ln;
        if (pid === 0x0A) text = val;
      } else {
        break;
      }
    }
    if (seq === null && text === null) return null;
    return { seq, text };
  }

  function buildQuickReplyList(items) {
    const out = [0x00, 0x00, items.length & 0xFF];
    for (const it of items) {
      const u = utf8(String(it || ""));
      out.push(0x01, u.length & 0xFF);
      for (let i = 0; i < u.length; i++) out.push(u[i]);
    }
    return new Uint8Array(out);
  }
  function buildQuickReplySwitch(enabled) {
    return new Uint8Array([0x02, 0x03, enabled ? 1 : 0]);
  }

  // rgba is size*size pixels, 4 bytes each, [R][G][B][255-alpha].
  function buildIconResponse(url, size, rgba) {
    const out = [];
    out.push(0x40 | MSG_NOTI_ICON);            // fixed + response -> 0x43
    const u = utf8(url);
    out.push(0x09, u.length & 0xFF);
    for (let i = 0; i < u.length; i++) out.push(u[i]);
    out.push(0x07);
    const L = le32(rgba.length);
    for (let i = 0; i < 4; i++) out.push(L[i]);
    for (let i = 0; i < rgba.length; i++) out.push(rgba[i]);
    out.push(0x23);
    const sz = le32(size & 0xFF);
    for (let i = 0; i < 4; i++) out.push(sz[i]);
    return new Uint8Array(out);
  }

  // Chunker for oversized ch 0x07 messages (rolling version id per transfer).
  class BigData {
    constructor() { this.version = 0; }
    chunk(data, cap) {
      cap = cap || BIGDATA_CAP;
      if (data.length <= cap) return [data];
      const ver = this.version;
      this.version = (this.version + 1) % 32;
      const dataCap = cap - 6;
      const chunks = [];
      let off = 0, idx = 0;
      while (off < data.length) {
        const end = Math.min(off + dataCap, data.length);
        const flag = idx === 0 ? 0 : (end === data.length ? 2 : 1);
        const head = new Uint8Array([BIGDATA_MARKER, ((ver << 3) | flag) & 0xFF, idx & 0xFF, (idx >>> 8) & 0xFF, (idx >>> 16) & 0xFF, (idx >>> 24) & 0xFF]);
        const c = new Uint8Array(6 + (end - off));
        c.set(head, 0);
        c.set(data.subarray(off, end), 6);
        chunks.push(c);
        off = end; idx++;
      }
      return chunks;
    }
  }

  function parseBigdataAck(body) {
    if (!body || body.length < 2) return null;
    const b = body[1];
    return { version: (b & 0xF8) >> 3, success: (b & 0x03) !== 0 };
  }

  // --------------------------------------------------------------------------
  // Media pushes (channel 0x09)
  // --------------------------------------------------------------------------
  function buildNowPlaying(title, artist, durationMs, albumArt, appId) {
    return Payload.variableResponse(MSG_NOW_PLAYING)
      .addLong(0x01, durationMs || 0)
      .addString(0x02, title || "")
      .addString(0x03, artist || "")
      .addString(0x04, albumArt || "")
      .addString(0x05, appId || "")
      .build();
  }
  function buildPlaybackState(playing, positionMs, queueItemId, appId) {
    return Payload.variableResponse(MSG_PLAYBACK_STATE)
      .addByte(0x01, playing ? STATE_PLAYING : STATE_PAUSED)
      .addLong(0x02, positionMs || 0)
      .addLong(0x04, queueItemId || 0)   // param order 1,2,4,3 is deliberate
      .addString(0x03, appId || "")
      .build();
  }
  function buildMediaVolume(volume, headsetConnected) {
    const out = [0x40 | MSG_MEDIA_VOLUME_PUSH, 2, 0x01];
    const v = le32(volume & 0xFFFFFFFF);
    for (let i = 0; i < 4; i++) out.push(v[i]);
    out.push(0x02, headsetConnected ? 2 : 1);
    return new Uint8Array(out);
  }
  function buildMediaCapability(maxVolume, warningVolume) {
    const out = [0x40 | MSG_MEDIA_CAPABILITY, 2, 0x01];
    const a = le32(maxVolume & 0xFFFFFFFF);
    for (let i = 0; i < 4; i++) out.push(a[i]);
    out.push(0x02);
    const w = le32(warningVolume & 0xFFFFFFFF);
    for (let i = 0; i < 4; i++) out.push(w[i]);
    return new Uint8Array(out);
  }

  function parseMediaButton(body) {
    if (!body || body.length < 2) return null;
    const hdr = body[0];
    if ((hdr & 0x3F) !== MSG_MEDIA_KEY) return null;
    const variable = (hdr >> 7) & 1;
    let i = variable ? 2 : 1;
    let keyCode = 0, keyAction = 0;
    while (i + 1 < body.length) {
      const pid = body[i], val = body[i + 1]; i += 2;
      if (pid === 0x01) keyCode = val;
      else if (pid === 0x02) keyAction = val;
      else break;
    }
    return { key_code: keyCode, key_action: keyAction };
  }

  function parseMediaVolume(body) {
    if (!body || body.length < 2) return null;
    const hdr = body[0];
    if ((hdr & 0x3F) !== MSG_MEDIA_VOLUME) return null;
    const variable = (hdr >> 7) & 1;
    let i = variable ? 2 : 1;
    let command = 0, value = 0;
    while (i + 1 < body.length) {
      const pid = body[i], val = body[i + 1]; i += 2;
      if (pid === 0x01) command = val;
      else if (pid === 0x02) value = val;
      else break;
    }
    return { command, value };
  }

  function parseBatteryResponse(body) {
    if (!body || body.length < 1) return null;
    const msgId = body[0] & 0x3F;
    const isResponse = (body[0] >> 6) & 1;
    const isVariable = (body[0] >> 7) & 1;
    if (msgId !== MSG_BATTERY || isVariable || !isResponse) return null;
    let level = -1, charging = false;
    let i = 1;
    while (i + 1 < body.length) {
      const pid = body[i], val = body[i + 1]; i += 2;
      if (pid === 0x05) level = val;
      else if (pid === 0x06) charging = val !== 0;
    }
    return { level, charging };
  }

  // --------------------------------------------------------------------------
  // Weather builder (channel 0x05, MSG_ID 1). Weather is the odd one out: header
  // followed by a 2-byte BIG-endian param count; fixed-width numerics carry no
  // length byte - only strings do. Keep the forecast short so it stays under one
  // SAP frame (this band dislikes fragmented messages).
  // --------------------------------------------------------------------------
  function buildWeather(w, fahrenheit, uvText) {
    fahrenheit = !!fahrenheit; uvText = uvText || "Low";
    const out = [];
    let n = 0;
    function wb(pid, v) { out.push(pid & 0xFF, int(v) & 0xFF); n++; }
    function w16(pid, v) {
      out.push(pid & 0xFF);
      const e = le16(int(v) & 0xFFFF); out.push(e[0], e[1]);
      n++;
    }
    function w32(pid, v) {
      out.push(pid & 0xFF);
      const e = le32(int(v) & 0xFFFFFFFF); out.push(e[0], e[1], e[2], e[3]);
      n++;
    }
    function ws(pid, s) {
      const u = utf8(s || "");
      out.push(pid & 0xFF, u.length & 0xFF);
      for (let i = 0; i < u.length; i++) out.push(u[i]);
      n++;
    }
    function int(x) { return Math.round(Number(x) || 0); }

    // settings block
    wb(0x08, 1); wb(0x06, fahrenheit ? 1 : 0); wb(0x09, 0); wb(0x0A, 1); wb(0x07, 0); wb(0x0B, 1);
    // current conditions
    wb(0x0C, 0); wb(0x12, 1);
    ws(0x0D, w.city); ws(0x0E, w.region); ws(0x0F, w.country);
    wb(0x10, w.icon);
    w32(0x13, bandEpoch(w.time));
    w16(0x14, w.temp); w16(0x15, w.high); w16(0x16, w.low);
    wb(0x11, w.is_day);
    w16(0x18, w.high); w16(0x19, w.low);
    w32(0x1A, bandEpoch(w.sunrise)); w32(0x1B, bandEpoch(w.sunset));
    ws(0x1C, "");
    w16(0x17, w.feels_like);
    // hourly
    wb(0x1D, w.hourly.length);
    for (let i = 0; i < w.hourly.length; i++) {
      const h = w.hourly[i];
      wb(0x1E, i); wb(0x22, h.is_day); w32(0x1F, bandEpoch(h.t));
      wb(0x20, h.icon); w16(0x21, h.temp); wb(0x23, h.precip);
    }
    // daily
    wb(0x24, w.daily.length);
    for (let i = 0; i < w.daily.length; i++) {
      const d = w.daily[i];
      wb(0x25, i); w32(0x26, bandEpoch(d.t));
      wb(0x27, d.icon); wb(0x28, d.icon); wb(0x29, d.icon);
      w16(0x2A, d.max); w16(0x2B, d.min);
    }
    // air quality / UV / wind / humidity (no AQI source -> zeros)
    w16(0x2C, 0); w16(0x2D, 0); w16(0x2E, 0); w16(0x2F, 0); w16(0x30, 0); w16(0x31, 0);
    w16(0x32, w.uv); ws(0x33, uvText);
    w16(0x34, w.wind_speed); ws(0x35, w.wind_dir);
    w16(0x36, w.humidity);

    // header: variable + response + MSG_ID 1, then the 2-byte BE param count
    return new Uint8Array([0xC1, (n >> 8) & 0xFF, n & 0xFF].concat(out));
  }

  function wmoToSamsungIcon(code) {
    if (code === 0) return 0;
    if (code === 1 || code === 2) return 1;
    if (code === 3) return 2;
    if (code === 45 || code === 48) return 3;
    if (code === 51 || code === 53 || code === 55) return 5;
    if (code === 56 || code === 57) return 16;
    if (code === 61) return 5;
    if (code === 63 || code === 65) return 4;
    if (code === 66 || code === 67) return 16;
    if (code === 71 || code === 77) return 10;
    if (code === 73 || code === 75) return 13;
    if (code === 80 || code === 81 || code === 82) return 5;
    if (code === 85 || code === 86) return 13;
    if (code === 95 || code === 96 || code === 99) return 8;
    return 1;
  }
  function uvLabel(uv) {
    if (uv < 3) return "Low";
    if (uv < 6) return "Moderate";
    if (uv < 8) return "High";
    if (uv < 11) return "Very High";
    return "Extreme";
  }
  function compass(deg) {
    const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    return dirs[Math.round(deg / 45) % 8];
  }

  // --------------------------------------------------------------------------
  // Watchface (bandface) channel 0x06
  // --------------------------------------------------------------------------
  const BF_GET_ALL_INFO = 0, BF_GET_BANDFACE_LIST = 1, BF_GET_CURRENT_BANDFACE = 2,
        BF_SET_CURRENT_BANDFACE = 3, BF_INSTALL_BANDFACE = 4, BF_DELETE_BANDFACE = 5,
        BF_GET_EDIT_INFO = 6, BF_GET_CURRENT_ORDER = 9, BF_GET_IMAGE_LIST = 14;
  const BF_P_NAMES = {};
  const bfParams = {
    0: "CURRENT_WF_ID", 1: "MAX_WATCHFACE_COUNT", 2: "WF_COUNT", 3: "WATCHFACE_LIST",
    4: "WF_ID", 5: "WF_VERSION", 6: "WF_NAME", 7: "WF_DESCRIPTION", 8: "STYLE_ID",
    9: "POSITION_INDEX", 10: "STYLE_INDEX", 11: "IS_CURRENT", 12: "IS_EDITABLE",
    13: "CLOCK_TYPE", 14: "HOUR_INDEX", 15: "MINUTE_INDEX", 16: "SECOND_INDEX",
    17: "WIDGET_INDEX", 18: "WIDGET_TYPE", 19: "BACKGROUND", 20: "IMAGE_ID_LIST",
    21: "WF_ID_LIST", 22: "INSTALL_RESULT", 23: "UNINSTALL_RESULT", 24: "NUM_OF_PARAMS",
    25: "WF_CURRENT_ORDER", 26: "RESULT_STATUS", 27: "IMAGE_COUNT", 28: "IMAGE_NAME",
    29: "WF_SAMPLER_ID", 30: "CLOCK_COLOR", 31: "IS_SELECTED",
  };
  const BF_MSG_NAMES = {
    0: "GET_ALL_INFO", 1: "GET_BANDFACE_LIST", 2: "GET_CURRENT_BANDFACE",
    3: "SET_CURRENT_BANDFACE", 4: "INSTALL_BANDFACE", 5: "DELETE_BANDFACE",
    6: "GET_EDIT_INFO", 7: "SET_EDIT_INFO", 8: "CANCEL_EDIT_INFO", 9: "GET_CURRENT_ORDER",
    10: "SET_CURRENT_ORDER", 11: "ADD_IMAGE_LIST", 12: "DELETE_IMAGE_LIST", 14: "GET_IMAGE_LIST",
  };
  const BF_STRING_PARAMS = { 5: true, 6: true, 7: true, 28: true };

  function bfMsg(msgId, params) {
    const out = [msgId & 0x3F];
    for (const p of (params || [])) out.push(p[0] & 0xFF, p[1] & 0xFF);
    return new Uint8Array(out);
  }
  function bfP(paramId, value) { return [paramId & 0xFF, value & 0xFF]; }

  const bandfaceBuilders = {
    getAllInfo: function () { return bfMsg(BF_GET_ALL_INFO); },
    getBandfaceList: function () { return bfMsg(BF_GET_BANDFACE_LIST); },
    getCurrentBandface: function () { return bfMsg(BF_GET_CURRENT_BANDFACE); },
    setCurrentBandface: function (wfId, samplerId) {
      return bfMsg(BF_SET_CURRENT_BANDFACE, [bfP(4, wfId), bfP(29, samplerId || 0)]);
    },
    getCurrentOrder: function () { return bfMsg(BF_GET_CURRENT_ORDER); },
  };

  function parseBandfaceResponse(body) {
    const out = { raw: hex(body) };
    if (!body || body.length < 1) return out;
    const hdr = body[0];
    out.msg_id = hdr & 0x3F;
    out.msg = BF_MSG_NAMES[hdr & 0x3F] || ("msg" + (hdr & 0x3F));
    out.is_response = !!((hdr >> 6) & 1);
    let i = 1;
    while (i < body.length) {
      const pid = body[i]; i += 1;
      if (pid === 3) {  // WATCHFACE_LIST
        if (i >= body.length) break;
        const n = body[i]; i += 1;
        const faces = [];
        for (let k = 0; k < n; k++) {
          const parsed = parseBfEntry(body, i);
          if (!parsed.wf) break;
          faces.push(parsed.wf);
          i = parsed.next;
        }
        out.watchfaces = faces;
        continue;
      }
      if (i >= body.length) break;
      const name = bfParams[pid] || ("p" + pid);
      if (BF_STRING_PARAMS[pid]) {
        const ln = body[i]; i += 1;
        if (i + ln > body.length) break;
        out[name] = decodeUtf8(body.subarray(i, i + ln)).replace(/\0+$/, "");
        i += ln;
      } else {
        out[name] = body[i]; i += 1;
      }
    }
    return out;
  }

  function parseBfEntry(body, i) {
    // NUM_OF_PARAMS-prefixed watchface entry.
    if (i >= body.length || body[i] !== 24 || i + 1 >= body.length) return { wf: null, next: i };
    const count = body[i + 1];
    i += 2;
    const wf = {};
    for (let k = 0; k < count; k++) {
      if (i >= body.length) break;
      const pid = body[i]; i += 1;
      const name = bfParams[pid] || ("p" + pid);
      if (BF_STRING_PARAMS[pid]) {
        if (i >= body.length) break;
        const ln = body[i]; i += 1;
        if (i + ln > body.length) break;
        wf[name] = decodeUtf8(body.subarray(i, i + ln)).replace(/\0+$/, "");
        i += ln;
      } else {
        if (i >= body.length) break;
        wf[name] = body[i]; i += 1;
      }
    }
    return { wf, next: i };
  }

  // --------------------------------------------------------------------------
  // Exports
  // --------------------------------------------------------------------------
  window.SAP = {
    // generic byte helpers
    crc16, toBytes, concat, hex, hexSpace, be16, le16, le32, le64,
    readU16BE, readU32LE, utf8, decodeUtf8,
    // transport / handshake
    frameWrap, frameUnwrap, frameIsHello, helloIsBand,
    parseBandHello, buildPhoneHello, randomToken,
    // session
    packetEncodeAck, packetEncodeContinuation, packetEncodeFirstFragment,
    packetEncodeSingle, packetParse, buildFrames,
    // payload builder
    Payload,
    // constants
    CH_CONTROL, CH_WEATHER, CH_NOTIFICATION, CH_MEDIA, CH_SETTINGS, CH_BANDFACE,
    CH_QUICK_REPLY,
    MSG_NOTI_POPUP, MSG_NOTI_NO_POPUP, MSG_NOTI_DND, MSG_NOTI_ICON, MSG_NOTI_CLEAR,
    MSG_NOTI_ACTION, MSG_LARGE_DATA_ACK,
    ALERT_SILENT, ALERT_VIBRATION_ONLY, ALERT_SOUND_ONLY, ALERT_SOUND_AND_VIBRATION,
    BIGDATA_MARKER, BIGDATA_CAP,
    MSG_INIT_SETTING, MSG_LICENSE, MSG_BATTERY,
    MSG_MEDIA_STATE_REQ_A, MSG_MEDIA_STATE_REQ_B, MSG_MEDIA_KEY, MSG_MEDIA_CAPABILITY_REQ,
    MSG_MEDIA_VOLUME_REQ, MSG_MEDIA_VOLUME, MSG_NOW_PLAYING, MSG_PLAYBACK_STATE,
    KEY_STOP, KEY_PLAYPAUSE, KEY_PREVIOUS, KEY_NEXT, ACTION_PRESSED, ACTION_RELEASED,
    VOL_SET, VOL_UP, VOL_DOWN, VOL_MUTE_ON, VOL_MUTE_OFF, MEDIA_KEY_NAMES, VOL_CMD_NAMES,
    MSG_WEATHER_INFO, MSG_WEATHER_ADD_LOCATION,
    // builders / parsers
    buildBatteryRequest, buildLicenseRequest, buildInitSetting, buildNotification,
    parseIconRequest, parseNotificationClear, parseNotificationAction,
    buildQuickReplyList, buildQuickReplySwitch, buildIconResponse,
    buildNowPlaying, buildPlaybackState, buildMediaVolume, buildMediaCapability,
    parseMediaButton, parseMediaVolume, parseBatteryResponse,
    buildWeather, wmoToSamsungIcon, uvLabel, compass,
    BigData, parseBigdataAck,
    javaStringHash, appIdFor,
    // watchface (bandface)
    bandface: {
      CHANNEL: CH_BANDFACE,
      builders: bandfaceBuilders,
      parse: parseBandfaceResponse,
      MSG_NAMES: BF_MSG_NAMES,
    },
  };
})();
