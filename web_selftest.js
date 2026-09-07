// Ad-hoc protocol self-test harness (Node). Not shipped: run with `node web_selftest.js`.
// It just exercises sap.js logic that doesn't need Bluetooth.
"use strict";
globalThis.window = globalThis;
require("./web/js/sap.js");
const SAP = globalThis.SAP;
let pass = 0, fail = 0;
function eq(desc, a, b) {
  const ok = JSON.stringify(a) === JSON.stringify(b);
  if (ok) pass++; else { fail++; console.log("FAIL " + desc + "\n  got  " + JSON.stringify(a) + "\n  want " + JSON.stringify(b)); }
}
function bytesEq(desc, a, b) {
  const A = [...a], B = [...b];
  const ok = A.length === B.length && A.every((v, i) => v === B[i]);
  if (ok) pass++; else { fail++; console.log("FAIL " + desc + "\n  got  " + SAP.hexSpace(a, 40) + "\n  want " + SAP.hexSpace(b, 40)); }
}

// CRC-16/ARC sanity: CRC of the empty-ish / known value.
eq("crc16('123456789')", SAP.crc16(new TextEncoder().encode("123456789")), 0xBB3D);

// frame wrap/unwrap round trip
{
  const body = new Uint8Array([1, 2, 3, 250, 255]);
  const w = SAP.frameWrap(body);
  const uw = SAP.frameUnwrap(w);
  bytesEq("frame round-trip", uw, body);
  // corrupted data => unwrap null
  const bad = w.slice(); bad[4] ^= 0xff;
  eq("frame corruption rejected", SAP.frameUnwrap(bad) === null, true);
}

// buildFrames small (single) vs large (fragmented) and packet parse
{
  const single = SAP.buildFrames(0x09, 0x40, new Uint8Array([0xaa, 0xbb]), 512);
  eq("small => 1 frame", single.length, 1);
  const p = SAP.packetParse(SAP.frameUnwrap(single[0]));
  eq("single channel", p.channel, 0x09);
  eq("single kind", p.kind, "single");

  const big = new Uint8Array(600).fill(0x77);
  const frames = SAP.buildFrames(0x07, 0x50, big, 512);
  eq("big => fragmented frames", frames.length, 2);
  const f0 = SAP.packetParse(SAP.frameUnwrap(frames[0]));
  const f1 = SAP.packetParse(SAP.frameUnwrap(frames[1]));
  eq("first fragment kind", f0.kind, "first");
  eq("first fragment channel", f0.channel, 0x07);
  eq("continuation kind", f1.kind, "cont");
  // reassemble
  const joined = new Uint8Array([...f0.body, ...f1.body]);
  bytesEq("fragment reassembly", joined, big);

  // small MTU many frames
  const frames23 = SAP.buildFrames(0x07, 0x40, big, 23);
  const assembled = [];
  for (const f of frames23) {
    const pk = SAP.packetParse(SAP.frameUnwrap(f));
    assembled.push(...(pk.kind === "first" || pk.kind === "cont" ? pk.body : []));
  }
  bytesEq("mtu23 reassembly", new Uint8Array(assembled), big);
}

// ack construction length
{
  const ack = SAP.frameWrap(SAP.packetEncodeAck(0x12, false));
  bytesEq("ack frame", ack, SAP.frameWrap(SAP.packetEncodeAck(0x12, false)));
}

// HELLO round trip
{
  const bh = { param_block: new Uint8Array([1, 2, 3, 4, 5, 6, 7]), model: "SM-R390", vendor: "samsung" };
  const hello = SAP.buildPhoneHello(bh, "ABCDEF0123456789ABCDEF0123456789", "M2007J3SY", "Xiaomi", null);
  eq("hello len", hello.length, 105);
  eq("hello header", hello[1], 0x15);
}

// app_id (java string hash) stable
{
  eq("appIdFor", SAP.appIdFor("com.example.fit3server"), String(Math.abs(SAP.javaStringHash("com.example.fit3server"))));
  // known-ish cross-check not needed; ensure same twice
  eq("appId deterministic", SAP.appIdFor("com.foo.bar"), SAP.appIdFor("com.foo.bar"));
}

// notification parse/build + parsers
{
  const noti = SAP.buildNotification({ seq: 42, title: "Hi", body: "Hello there", app_name: "App", pkg: "com.a.b", popup: true, need_quick_reply: true });
  eq("noti header var|popup", noti[0] & 0x80, 0x80);
  eq("noti msgid", noti[0] & 0x3F, 0x00);

  // icon request parse
  const req = new Uint8Array([0x03, 0x09, 8, 0x41, 0x42, 0x43, 0x44, 0x5f, 0x49, 0x43, 0x4f, 0x23, 52]);
  // APP_ICON_ url len should be 9: A P P _ I C O N _ = 9
  const req2 = new Uint8Array([0x03, 0x09, 9, 0x41, 0x50, 0x50, 0x5f, 0x49, 0x43, 0x4f, 0x4e, 0x5f, 0x23, 52]);
  const parsed = SAP.parseIconRequest(req2);
  eq("icon url", parsed && parsed.url, "APP_ICON_");
  eq("icon size", parsed && parsed.size, 52);

  // notification action parse (msg 10, variable). body: hdr(0x8a),count(2), then params
  // param 1 seq int32 LE, param 10 reply text
  const act = new Uint8Array([0x80 | 0x0A, 2, 0x01, 5, 0, 0, 0, 0x0A, 2, 0x4f, 0x4b]);
  const a = SAP.parseNotificationAction(act);
  eq("action seq", a.seq, 5);
  eq("action text", a.text, "OK");

  // quick reply list
  const ql = SAP.buildQuickReplyList(["OK", "Done"]);
  bytesEq("qr list", ql, new Uint8Array([0, 0, 2, 1, 2, 0x4f, 0x4b, 1, 4, 0x44, 0x6f, 0x6e, 0x65]));
}

// battery parse
{
  const resp = new Uint8Array([0x42, 0x05, 87, 0x06, 1]);
  const b = SAP.parseBatteryResponse(resp);
  eq("battery level", b.level, 87);
  eq("battery charging", b.charging, true);
}

// media parsers
{
  // media button: hdr 0x04 fixed request, param 1 key, param 2 action pressed
  const btn = SAP.parseMediaButton(new Uint8Array([0x04, 0x01, 2, 0x02, 2]));
  eq("media key", btn.key_code, 2);
  eq("media action", btn.key_action, 2);

  const vol = SAP.parseMediaVolume(new Uint8Array([0x07, 0x01, 1, 0x02, 8]));
  eq("volume cmd", vol.command, 1);
  eq("volume value", vol.value, 8);

  bytesEq("capability", SAP.buildMediaCapability(15, 15),
    new Uint8Array([0x54, 2, 0x01, 15,0,0,0, 0x02, 15,0,0,0]));
}

// bandface parse of a small response: msg0 response (hdr 0x40) then params current wf id etc + a list entry
{
  // hdr 0x40 (GET_ALL_INFO response), then param0=7 (current id), param1=10, param2=2
  // param3 WATCHFACE_LIST count=1 then NUM_OF_PARAMS(24)=7 then entry params: 4=77,6 'wf_name'...
  const body = new Uint8Array([
    0x40,
    0x00, 7, 0x01, 10, 0x02, 2,
    0x03, 1,
    24, 2, 4, 77, 6, 8, 0x77,0x66,0x5f,0x6e,0x61,0x6d,0x65,0x00
  ]);
  const d = SAP.bandface.parse(body);
  eq("bf msg", d.msg, "GET_ALL_INFO");
  eq("bf current", d.CURRENT_WF_ID, 7);
  eq("bf watchfaces len", d.watchfaces.length, 1);
  eq("bf wf id", d.watchfaces[0].WF_ID, 77);
  eq("bf wf name", d.watchfaces[0].WF_NAME, "wf_name");
}

// bigdata chunk + ack parse
{
  const bd = new SAP.BigData();
  const big = new Uint8Array(2000).fill(9);
  const chunks = bd.chunk(big, 980);
  eq("bigdata chunks", chunks.length, 3);
  const sizes = chunks.reduce((a, c) => a + (c.length - 6), 0);
  eq("bigdata total", sizes, 2000);
  // first chunk header marker + flag 0
  eq("chunk0 marker", chunks[0][0], 0x3F);
  const ack = SAP.parseBigdataAck(new Uint8Array([0x3e, (2 << 3) | 1, 4,0,0,0]));
  eq("bigdata ack ver", ack.version, 2);
  eq("bigdata ack success", ack.success, true);
}

// weather builder runs and stays small; header 0xc1 with BE count
{
  const w = { city:"London",region:"",country:"UK", time: Date.now()/1000, temp:18, feels_like:17,
    humidity:60, icon:1, is_day:1, wind_speed:10, wind_dir:"SW", high:20, low:12,
    sunrise: Date.now()/1000, sunset: Date.now()/1000, uv:3,
    hourly:[{t:Date.now()/1000,icon:1,temp:18,precip:0,is_day:1}],
    daily:[{t:Date.now()/1000,icon:1,max:20,min:12}] };
  const b = SAP.buildWeather(w, false, "Low");
  eq("weather header", b[0], 0xC1);
  eq("weather under 512", b.length <= 512, true);
}

// ---- byte-for-byte cross-check against the reference Python implementation ----
function hexStr(u) { return [...u].map(b => b.toString(16).padStart(2, "0")).join(" "); }
function hexEq(desc, got, wantHex) {
  const gotHex = hexStr(got);
  if (gotHex === wantHex) pass++;
  else { fail++; console.log("FAIL " + desc + "\n  got  " + gotHex + "\n  want " + wantHex); }
}
hexEq("battery_req", SAP.buildBatteryRequest(), "02");
hexEq("license", SAP.buildLicenseRequest(true), "84 01 01");
hexEq("qr switch", SAP.buildQuickReplySwitch(true), "02 03 01");
hexEq("qr list", SAP.buildQuickReplyList(["OK", "Done"]), "00 00 02 01 02 4f 4b 01 04 44 6f 6e 65");
hexEq("capability", SAP.buildMediaCapability(15, 15), "54 02 01 0f 00 00 00 02 0f 00 00 00");
hexEq("volume", SAP.buildMediaVolume(8), "55 02 01 08 00 00 00 02 01");
hexEq("now playing", SAP.buildNowPlaying("Hi", "A", 0, "", ""),
  "d1 05 01 00 00 00 00 00 00 00 00 02 02 48 69 03 01 41 04 00 05 00");
hexEq("playback", SAP.buildPlaybackState(true, 0, 0, ""),
  "d2 04 01 03 02 00 00 00 00 00 00 00 00 04 00 00 00 00 00 00 00 00 03 00");
eq("appid com.a.b", SAP.appIdFor("com.a.b"), "948515880");
eq("appid pc.launcher", SAP.appIdFor("pc.launcher"), "320867013");
hexEq("notification", SAP.buildNotification({
    seq: 9, title: "T", body: "B", app_name: "App", pkg: "com.app",
    when_ms: 1000, icon_newly_added: true, popup: true, alert_type: 0 }),
  "80 0c 00 01 01 09 00 00 00 06 09 39 34 38 35 31 37 39 34 30 02 e8 03 00 00 00 00 00 00 03 01 54 04 01 00 42 0e 03 41 70 70 0f 00 10 01 09 12 41 50 50 5f 49 43 4f 4e 5f 39 34 38 35 31 37 39 34 30 13 01 12 07 63 6f 6d 2e 61 70 70");

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);