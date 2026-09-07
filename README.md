A Galaxy Fit 3 connector for PC, in Python.

> (Mostly vibe coded cuz I couldn't be bothered to do this all from scratch)
> There are a ton of random and useless features so here is Claude and DeekSeek cooperating to make a huge README cuz once again I couldn't be bothered

And before you ask, yes, I did code the notification icons myself for one sole reason
  > (Apple. But Bad.)

# Galaxy Fit 3 — PC server

Connect a Samsung Galaxy Fit 3 to a Windows PC over Bluetooth LE, without a phone
or the Galaxy Wearable app. Speaks Samsung's Accessory Protocol (SAP) directly.

> **Browser client** — there's also a static **GitHub Pages** app in [`web/`](web/)
> that re-implements SAP over **Web Bluetooth** (no Python, no server): scan/connect
> in Chrome, battery, a browser-hosted media remote, push notifications with icons,
> watchface list/switch, and a best-effort weather push. Windows-only features
> (volume, toasts, SendInput, Home Assistant) have no browser equivalent and are
> omitted. See [`web/README.md`](web/README.md). **Not yet validated on a physical
> band.**

## What works

| Feature | Status |
|---|---|
| Pair / connect, auto-reconnect | ✅ persistent link via `GattSession.MaintainConnection` |
| Battery level | ✅ polled every 10s |
| Now-playing (title/artist/state) | ✅ real Windows media session |
| Media buttons (play/pause, next, prev) | ✅ press/release handled, 1 action per press |
| Volume from the band | ✅ two-way: band drives Windows volume, PC reports level back so the slider matches |
| Weather | ✅ real forecast (Open-Meteo) with your location |
| Band command capture + remap | ✅ any `(channel, msg_id)` → your action |
| Notifications → band | ✅ forwards real Windows toasts (incl. ones already on screen) |
| App icons on notifications | ✅ real app logos, both sizes (52px list, 112px detail) |
| Clear on band → clear on PC | ✅ dismissing on the band dismisses the Windows toast |
| Quick-reply buttons → custom PC actions | ✅ repurposed as launcher buttons (open an app, run anything) |
| Nested menus + paging | ✅ `menu` navigation, auto Prev/Next paging |
| Remote mouse / keyboard | ✅ SendInput-driven mouse pad + 8-page keyboard |
| Watchfaces — list / switch active face | ✅ `--probe-bandface`, `--set-face ID` |
| Watchfaces — install a custom `.bin` | ❌ needs SAFT — see "Watchfaces" below |
| Album art | ❌ blocked — same SAFT transport |

## Requirements

```powershell
pip install -r requirements.txt
```
Windows 10/11, Python 3.9+. Windows will ask once to allow reading notifications
(Settings → Privacy & security → Notifications).

## Usage

```powershell
python server.py                # the server: connect, poll, forward, remap
python server.py --capture      # verbose: log every band message (to discover commands)
python server.py --test-notify  # send a test notification on connect
python server.py --no-forward   # don't forward Windows notifications
python battery.py --once        # just read the battery and exit

# put an arbitrary image on the band (quote multi-word values)
python server.py --image cat.png --title "Cat" --body "meow"
```

### Arbitrary images & animation

The notification icon is the **only** image path that works over BLE, so that's
the canvas: the band fetches it at 52px (list) and 112px (opened), and `--image`
puts any PNG/JPG there. Album art and custom watchfaces both ship their images
over SAFT, which has no BLE transport — see below.

Icons are **mutable after sending**: re-send the notification with the same
`SEQUENCE_ID` and `IMAGE_NEWLY_ADDED` (param 0x13) set, and the band re-fetches the
icon past its cache. That makes an animated display:

```powershell
python server.py --frames anim.gif --animate 1 --title "Flipbook"
python server.py --frames .\frames\ --animate 1        # or a folder of PNGs
```

**Frame rate ~1 fps.** The transfer is fast (23 chunks in ~0.02s with
`ICON_FAST_WRITE`); the floor is the round trip — we can only *answer* an icon
request, so each frame costs notify → request → send → ack. Below ~0.7s/frame you
just queue faster than the band asks. High-contrast silhouettes read best at 52px.
To resample a video to the band's rate:

```
ffmpeg -i in.mp4 -vf "fps=1,scale=112:112:force_original_aspect_ratio=decrease" frames/f%04d.png
```

`ICON_FAST_WRITE` uses write-without-response (no per-chunk ATT round trip). It has
no flow control, so a band that fell behind would show up as `success=False` in the
big-data ack — watch those; set it `False` if you ever see nacks.

## Files

| File | What it does |
|------|--------------|
| `server.py` | The server: connection, polling, media, notifications, action dispatch. |
| `input_control.py` | Win32 SendInput: mouse move/click/scroll, key taps, unicode typing. |
| `bandface.py` | Watchface control (ch 0x06): list installed faces, read/switch the active one. |
| `sap.py` | Samsung Accessory Protocol: framing/CRC, HELLO, session+ACK, fragmentation, message builders. |
| `saft.py` | Samsung Accessory File Transfer (album art). Decoded and implemented, but **not reachable** — see "Album art". |
| `config.json` | **Button config** — quick-reply buttons + channel/msg remaps. Edit this, not code. (`config.example.json` is the template.) |
| `actions.py` | Python side: the launch helper + custom `@register`ed functions that `config.json` references by name. |
| `media.py` | Reads the current Windows media session (now playing). |
| `volume.py` | Windows master volume (pycaw): get/set/step/mute, plus band-unit conversion (`BAND_MAX`). |
| `weather.py` | Location (IP geolocation) + forecast (Open-Meteo). Set `LOCATION_OVERRIDE` to skip geolocation. |
| `winnoti.py` | Reads Windows toast notifications + app logos. |
| `icons.py` | Renders icons into the band's pixel format. |
| `battery.py` | Standalone battery reader. |
| `connect_winrt.py`, `scan.py` | Diagnostics: GATT dump / over-the-air scan. |

## Configuring buttons (config.json)

All button config lives in `config.json` (copy `config.example.json`). Two
sections: **`quick_replies`** (the launcher buttons, order = on-band order) and
**`actions`** (remaps of a physical band button, keyed by `channel` + `msg`).

Each entry picks exactly one action type:

| type | meaning |
|------|---------|
| `launch` | an app name or absolute path, started detached (like double-click) |
| `menu` | navigate to another menu (`""` = root) — see Menus below |
| `shell` | a raw command line, run detached |
| `run` | the name of a Python function registered in `actions.py` |

```json
{
  "quick_replies": [
    { "label": "Firefox", "launch": "firefox" },
    { "label": "YouTube", "shell": "start \"\" \"https://www.youtube.com/\"" },
    { "label": "Fetch",   "run": "fetch_homeassistant" }
  ],
  "actions": [
    { "channel": "0x1a", "msg": 0, "run": "shutdown", "debounce": 0 }
  ]
}
```

Built-in `run` names: `shutdown`, `media_playpause`, `action_center`,
`fetch_homeassistant`, `notification`. `channel` takes a hex string (`"0x1a"`) or
an int; `debounce` (seconds, optional, `actions` only) ignores repeat presses
within the window — normally 0, since the media button's apparent "double send"
is really PRESSED + RELEASED and the server already acts on PRESSED only.

A missing/invalid `config.json` degrades to "no buttons" rather than crashing;
a single bad entry is skipped with a warning and the rest still load.

**To find a band button's `channel`/`msg`:** `python server.py --capture`, press
it, read the `CMD ch=0x.. msg=..` line.

### Adding custom behaviour

When `launch`/`shell` aren't enough, write a function in `actions.py` and register
a name for it — then reference that name from `config.json` via `"run"`:

```python
@register("lock")
def action_lock(ctx):
    subprocess.Popen("rundll32 user32.dll,LockWorkStation", shell=True)
```
```json
{ "label": "Lock", "run": "lock" }
```

`ctx` includes `ctx['notify'](title, body, ...)` to push a notification back to the
band (see `fetch_homeassistant`). Do slow work on a thread so it doesn't stall the
server.

### Menus, paging, and remote mouse/keyboard

A button with `"menu": "name"` **navigates** instead of running something: the
server swaps the band's quick-reply list and re-arms the launcher. Since
quick-reply lists are mutable mid-session, nested screens just work.

```json
"quick_replies": [
  { "label": "Mouse", "menu": "mouse" }
],
"menus": {
  "mouse": {
    "page_size": 9,
    "buttons": [
      { "label": "Right", "run": "mouse_move", "args": { "dx": 40 } },
      { "label": "Back",  "menu": "" }
    ]
  }
}
```

`""` is the root menu. Actions resolve **within the current menu**, so several
menus can each have their own `Back`.

**Paging** is automatic: a menu with more buttons than `page_size` reserves its
last two slots for `< Prev` / `Next >`, which wrap around so every page has the
same layout. The shipped `keyboard` menu is 52 buttons over 8 pages.

**`args`** lets one registered function serve many buttons — 26 letters don't need
26 functions:

| run | args | does |
|-----|------|------|
| `mouse_move` | `dx`, `dy` | relative cursor move |
| `mouse_click` | `button` (left/right/middle), `double` | click |
| `mouse_scroll` | `amount`, `horizontal` | wheel |
| `key` | `key` (VK name or char), `mods` (`["ctrl"]`) | tap a key / shortcut |
| `type` | `text` | type a literal string |

Input goes through Win32 `SendInput` (`input_control.py`), so games and browsers
respect it, and `type` uses UNICODE scancodes — layout-independent, any character.

Two honest caveats:

- **Latency.** Each press is a full reply cycle: tap → band deletes → re-arm
  (~1–2s) → reopen. Realistically ~5s per input. Great for "click this" or a
  shortcut; genuinely painful for typing a sentence. Prefer `type` buttons with
  whole words/phrases over letter-by-letter, and `key` shortcuts over navigation.
- **Pointer acceleration.** Windows scales relative mouse moves (a requested
  40,25 measured as 50,31), so `dx`/`dy` are approximate — tune to taste.

### Context-sensitive buttons (`when`)

A `when` condition makes a button appear only when it's currently true. No `when`
= always shown. The list is re-evaluated **every time the launcher arms**, so
buttons appear/vanish live (quick-reply lists are mutable mid-session). Exactly
one condition type per `when`:

```json
{ "label": "Surprise", "launch": "firefox", "when": { "random": 0.25 } }
```

Put `when` on a single button, or on a **group** to share it across several — a
group is `{ "when": ..., "buttons": [ ... ] }` and reads like an `if` block.
Groups nest, and a button's own `when` is AND-ed with its enclosing group's:

```json
{ "when": { "time": { "from": "09:00", "to": "18:00" } },
  "buttons": [
    { "label": "VS Code", "launch": "code" },
    { "label": "Slack",   "launch": "slack" }
  ] }
```

- `time` — an inclusive window; wraps past midnight if `from` > `to`.
- `random` — probability 0–1, rolled each arm.
- `predicate` — the name of a bool-returning function registered with
  `@predicate("name")` in `actions.py`. Keep predicates **fast/non-blocking** —
  they run on every arm. (This is the escape hatch for anything: HA state, files,
  window titles, etc. — cache slow lookups.)

Add a new condition type by registering an evaluator in `CONDITION_TYPES`
(`actions.py`). A broken/unknown condition hides its button with a warning rather
than crashing.

The quick-reply list is pushed to the band on every connect, along with a
**persistent launcher notification** ("PC Actions") whose only job is to host the
buttons — open it on the band and reply to run an action.

The band **deletes a notification once it's been replied to**, and the user can
swipe it away, either of which would make the buttons one-shot. So the server
re-sends the launcher ~1s after it disappears (both arrive as a dismiss message we
already handle). It reuses one `SEQUENCE_ID`, so it updates in place instead of
stacking, and re-arms silently — `LAUNCHER_ALERT = sap.ALERT_SILENT` is what stops
the buzz, *not* `LAUNCHER_POPUP` (see ALERT_TYPE under Protocol notes).

If the band ever rejects it and deletes instantly, a guard stops after
`LAUNCHER_MAX_RESENDS` in `LAUNCHER_RESEND_WINDOW` rather than looping forever at
one notification per second. Disable the whole thing with `--no-launcher`.

### Actions can push notifications back (Home Assistant)

Every action gets `ctx['notify'](title, body, app_name=..., alert=True)` — a
thread-safe way to send a notification *to* the band from within an action. The
`Fetch` quick-reply demonstrates it: it pulls live entity states from Home
Assistant and shows them on your wrist (with a buzz).

Set it up by copying `ha_config.example.json` → `ha_config.json` and filling in:

```json
{
  "url": "http://homeassistant.local:8123",
  "token": "<long-lived access token>",
  "title": "Home",
  "entities": [
    { "entity_id": "sensor.living_room_temperature", "label": "Living Room" }
  ]
}
```

`homeassistant.py` uses the HA REST API (`GET /api/states/<id>`, stdlib only).
Test it standalone with `python homeassistant.py`. The token is a credential —
`ha_config.json` stays local (it's your own HA → your own band, nothing leaves
your network). `HA_URL` / `HA_TOKEN` env vars override the file if set.

To wire your own fetch-and-display action, follow `action_fetch_homeassistant`
in `actions.py`: do the slow work on a thread, then call `ctx['notify'](...)`.

## Protocol notes

SAP rides on GATT service `00001a1a`: notify `797ae4e9` (band→phone), write
`63e30bad` (phone→band). Connect by address via `FromBluetoothAddressAsync` —
the band doesn't advertise once bonded, so scanning won't find it.

```
transport  [LEN:2 BE][HDR_CRC:2 BE][PAYLOAD][DATA_CRC:2 BE]      CRC-16/ARC, init 0
session    [TYPE][SEQ][CHAN][CHAN][BODY]   + [0x88][SEQ][FLAG][00] ACKs
           fragmented as FIRST(0x03) + CONTINUATION(0x07) when > MTU-3
message    [HDR][params]    HDR: bit7 variable, bit6 response, bits5-0 msg id
big data   [0x3f][(ver<<3)|flag][idx:4 LE][data]   for oversized ch 0x07 payloads
ack        [0x3e][(ver<<3)|success][chunk idx:4 LE]
```

**Big-data chunk cap:** the reference uses 980 bytes, but this band cannot
reassemble a big-data chunk that SAP had to fragment — a 50KB icon nacked at
chunk 25/52 every time. Cap each chunk to fit ONE SAP frame (`mtu - 3 - 6 - 4`,
i.e. ~499B at MTU 512) and it succeeds. The trailing int32 in the ack is the last
chunk index accepted — on failure, where it stopped; invaluable for debugging.

Handshake: band sends a plaintext HELLO (`0x68 0x14 … "SAMSUNG_ACCESSARY"`); reply
with a phone HELLO (`0x68 0x15 …`) echoing its param block, then `INIT_SETTING`
and `LICENSE_AGREEMENT` on ch 0x01 before it services other channels.

### Channel map

Confirmed against Samsung's own `hmaccessoryservices_new.xml` (serviceId == channel):

| ch | service | ch | service |
|----|---------|----|---------|
| 0x01 | OOBE / control | 0x0b | FullSettings (battery) |
| 0x02 | FindMyDevice | 0x0c | Debug |
| 0x03 | Call | 0x0f | Capability |
| 0x04 | LBS (location) | 0x12 | QuickMessage |
| 0x05 | Weather | 0x18 | AppsSAPAgent |
| 0x07 | Notification | 0x1a | SharedInfo |
| 0x08 | Calendar/time | 0x1e | OtaTransfer |
| 0x09 | Music | 0x0a | Health |

Key messages: battery = ch 0x0b msg 2 (`02` → `42 … 05 <level> 06 <charging>`);
now-playing = ch 0x09 msg 17; playback state = ch 0x09 msg 18; media button =
ch 0x09 msg 4; volume = ch 0x09 msg 7 (`01 <cmd>`, `02 <value>`; cmd 1=set 2=up
3=down 4=mute-on 5=mute-off); notification = ch 0x07 msg 0; icon request/response
= ch 0x07 msg 3; notification cleared on band = ch 0x07 msg 2 (`02 01 <seq:4 LE>`,
echoing the SEQUENCE_ID we sent); quick-reply tapped = ch 0x07 msg 10 (see below);
big-data ack = ch 0x07 msg 0x3e; weather = ch 0x05 msg 1.

### Notification channel (0x07) message ids

From Samsung's own `NotiPacketConstants.CommandType` (fit3plugin), which names
every message we'd only had raw bytes for before:

| msg | name | msg | name |
|-----|------|-----|------|
| 0 | NOTI_NEW_WITH_POPUP | 13 | NOTI_CAPABILITY_HANDSHAKE |
| 1 | NOTI_DELETE_FROM_MOBILE | 14 | NOTI_SYNC_DATA_AFTER_CONNECT |
| 2 | NOTI_DELETE_FROM_BAND | 16 | NOTI_UNBLOCK_APP |
| 3 | NOTI_ADDITIONAL_INFO (icon) | 17 | LAUNCH_NOTI_SETTINGS |
| 4 | NOTI_SHOW_ON_DEVICE | 18 | NOTI_WITH_DND |
| 5 | NOTI_NEW_WITH_NO_POPUP | 19 | SYNC_APP_ICON |
| 7 | NOTI_LAUNCH_APP_BY_APPID | 22 | SYNC_NOTI_SETTINGS |
| 8 | NOTI_CLEAR_ALL_FROM_MOBILE | 62 (0x3e) | LARGE_DATA_ACK_NACK |
| 9 | NOTI_CLEAR_ALL_FROM_BAND | | |
| **10** | **NOTIFICATION_ACTION** (quick reply) | | |
| 11 | NOTI_BLOCK_APP | | |

`NOTIFICATION_ACTION` (msg 10) is VARIABLE format (header bit 0x80 set) with a
param-count byte, then TLV params — `SEQUENCE_ID` (id 1, fixed int32 LE, no
length byte) and `REPLY_MESSAGE` (id 10, `[len:1][utf8]`) are the ones we use;
the band sends the literal button text, not an index.

**ALERT_TYPE (param 0x0F) is what makes the band buzz** — *not* the popup/no-popup
MSG_ID, which only controls whether a popup card is shown. They're independent.
Values from Samsung's `NotiDBConstants.AlertType`:

| value | meaning |
|-------|---------|
| 0 | SILENT |
| 1 | VIBRATION_ONLY |
| 2 | SOUND_ONLY |
| 3 | SOUND_AND_VIBRATION |

The reference hardcodes `1` for every notification, so a "no popup" notification
still vibrates. Pass `alert_type=sap.ALERT_SILENT` for notifications that should
appear without buzzing (the launcher does this).

### Quick-reply channel (0x12)

Not part of the notification channel — its own SAP service (`QuickMessageService`,
serviceId 18 == 0x12). Both messages are **FIXED format with no param-count byte**
(`SAMessageData.NO_OF_PARAM` is never set), ported from Samsung's own
`SAMessageDataPacketConstructor`:

```
switch on/off  msg 2:  02 | 03 <0 or 1>                     (verified byte-exact:
                                                               captured "02 03 01")
set list       msg 0:  00 | 00 <count> | 01 <len><utf8> | 01 <len><utf8> | ...
```

### Media channel (0x09) message ids

From Samsung's own `SMediaPacketInfo`. Requests (band→phone) are 1-9 and 16;
**phone→band pushes use the request id + 15**:

| req | name | push |
|-----|------|------|
| 1 | MEDIACHANGED | 16 |
| 2 | METADATA | **17** (now playing) |
| 3 | PLAYBACKSTATE | **18** |
| 4 | REMOTE_CONTROL (buttons) | — |
| 5 | CAPABILITY | **20** (`01 maxVolume:4LE`, `02 warningVolume:4LE`) |
| 6 | VOLUME_REQ | **21** (`01 volume:4LE`, `02 headset 2=yes/1=no`) |
| 7 | VOLUME_CONTROL | — |
| 8 | APP_INFO | 23 |
| 9 | QUEUE | 24 |

**Header/param-count rule (important):** the format bit (0x80) and the param-count
byte are *independent*. Samsung's `SAMessageData.build()` writes the count byte
whenever `NO_OF_PARAM > 0`, regardless of format — which is why the reference
needed a `variableNoCount` special case. Param width follows the Java overload:
`addParam(id, int)` = **4 bytes LE**, `addParam(id, byte)` = 1 byte.

**Volume units:** the band uses **Android's 0-15 media-volume scale**, not percent,
and it does *not* rescale to whatever `maxVolume` you announce — a full slider drag
sends ~15 relative up/down steps regardless. So report volume in band units
(`volume.BAND_MAX`, default 15) and convert to/from Windows percent; announce the
same value as `maxVolume`. Sending percent makes each band notch move the PC by
1%, so a full drag only nudges it a few percent.

Volume sync: push msg 21 on connect, on `VOLUME_REQ` (msg 6), and after every
change. The band only sends relative up/down, so without the echo its slider
drifts from the PC.

**Weather is the odd one out:** header `0xc1`, then a **2-byte BIG-endian** param
count (everything else here is little-endian), and fixed-width numeric params carry
no length byte — only strings do. Keep the forecast short (6 hourly / 4 daily) so
the message stays under one SAP frame; this band dislikes fragmented messages.

## Watchfaces (`bandface.py`, channel 0x06)

Watchface **control works** — listing, reading, and switching the active face are
all implemented and tested on hardware. Only *installing a new `.bin`* is blocked
(see below).

```powershell
python server.py --probe-bandface        # list installed faces + which is active
python server.py --set-face 77           # switch the active watchface
```

Ported from Samsung's own `bandface.messenger.worker.builder.*`. Wire format is the
shared `SAMessageData` one, except these builders never call `setNumberOfParam()`,
so there is **no param-count byte** — unlike media/notification. Header is
`(format<<7)|(type<<6)|msgId`, then raw `[paramId][value]`; string params
(`WF_VERSION`, `WF_NAME`, `WF_DESCRIPTION`, `IMAGE_NAME`) are `[id][len][utf8]`.

| msg | command | msg | command |
|-----|---------|-----|---------|
| 0 | GET_ALL_INFO | 7 | SET_EDIT_INFO |
| 1 | GET_BANDFACE_LIST | 8 | CANCEL_EDIT_INFO |
| 2 | GET_CURRENT_BANDFACE | 9 | GET_CURRENT_ORDER |
| 3 | **SET_CURRENT_BANDFACE** | 10 | SET_CURRENT_ORDER |
| 4 | INSTALL_BANDFACE | 11 | ADD_IMAGE_LIST |
| 5 | DELETE_BANDFACE | 12 | DELETE_IMAGE_LIST |
| 6 | GET_EDIT_INFO | 14 | GET_IMAGE_LIST |

`GET_ALL_INFO` returns `CURRENT_WF_ID`, `MAX_WATCHFACE_COUNT` (10 on this band),
`WF_COUNT`, then a `WATCHFACE_LIST` of `NUM_OF_PARAMS`-prefixed entries.

### Installing a custom watchface — still blocked

`.bin` watchfaces are **1.6–4.1 MB** (they live in the plugin APK at
`assets/watchface/SM_R390/<name>/<name>.bin`), far past the notification-icon path's
~50 KB ceiling, so they must go over SAFT — which needs a SAP service-connection
that we don't implement (see "SAFT" below).

Tested: sending `INSTALL_BANDFACE` alone returns `INSTALL_RESULT=1` (`SUCCESSFUL`,
per `BandFaceInstallStatus`) but `WF_COUNT` does **not** change and no file-transfer
session opens — the band is acking the request, still expecting the `.bin` first.

The community has fully reverse-engineered the `.bin` format itself:
[Ahmadjerj/galaxy-fit3-parser](https://github.com/Ahmadjerj/galaxy-fit3-parser)
(OPPO-derived container, 256×402, parses/renders every widget type). So the file
format is a solved problem — **only the transport is missing.**

## SAFT / album art (blocked)

Samsung ships bulk files over SAFT (Samsung Accessory File Transfer,
`/system/filetransfer`, channels 100 = JSON command, 101 = data). Opening it needs a
SAP **service-connection creation** exchange (capability discovery → session
allocation → connection request) at a layer this project never implemented — we ride
sessions the band opens itself. A blind connection request got no reply at all.

- **Album art**: `SMediaFileTransfer.sendAlbumArtFile()`, 256×402 PNG quality 50.
- **Watchfaces**: `bandface/filetransfer/ImageFilesSender` → `sendFileTransferReq(appId=6, …)`.

Images per se are *not* blocked — notification icons reach the band fine over BLE
via the message channel + big-data chunking (see `--image`). It's specifically the
bulk file transport that has no BLE route.

## Credit

SAP was reverse-engineered by the Gadgetbridge project; `sap.py` ports HighwayStar's
`samsung-fit3` branch (AGPL-3.0). Channel map and album-art details came from
decompiling Samsung's own `com.samsung.wearable.fit3plugin` and `com.samsung.accessory`.
