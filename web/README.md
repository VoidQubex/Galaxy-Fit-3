# Galaxy Fit 3 — browser client (GitHub Pages)

A static, no-backend web app that talks to the band directly from a browser via
**Web Bluetooth**, re-implementing Samsung's Accessory Protocol (SAP) in JavaScript.
It is the browser sibling of the Python `server.py` in this repo: same protocol,
same channel map, no Python, no local server.

> **Status: ported faithfully but not yet validated on a physical band.** The byte
> encoding is cross-checked against the Python implementation (see
> `web_selftest.js`), but a real Galaxy Fit 3 is required to confirm the two live
> paths below.

## What works here (browser-native only)

| Feature | Notes |
|---|---|
| Scan / connect | Picks the band in Chrome's Bluetooth chooser, subscribes, handshakes (HELLO → `INIT_SETTING` → license) |
| Battery | Polled every 10 s while connected + a manual refresh button |
| Now-playing media | The page hosts a small media session and reports it; band play/pause/next/volume drive the page. Add an audio file/URL for real progress |
| Push a notification | Title/body/app with an optional icon (drawn to the band's 52/112 px format) and optional quick-reply buttons |
| Watchfaces | List installed faces and switch the active one (channel 0x06) |
| Weather | Best-effort Open-Meteo push to the band's weather widget |

## What's intentionally skipped (no browser equivalent)

The Python server's Windows-only behaviour has no web equivalent and is left out:
master volume (pycaw), Windows toast forwarding (`winnoti.py`), `SendInput`
mouse/keyboard (`input_control.py`), and Home Assistant host actions (`actions.py`,
`homeassistant.py`).

What's blocked in **both** versions is also absent: album art and custom-`.bin`
watchface *installation*, both of which need the SAFT bulk-file transport that has
no BLE route.

## Requirements & caveats

- A **Chromium** browser with Web Bluetooth (Chrome / Edge). The API needs a
  **secure context** — GitHub Pages (`https`) or `localhost` is fine.
- **Discoverability.** Web Bluetooth can only reach devices that are currently
  *advertising*. Like the Python tool notes, once the band is bonded to a phone or
  PC it stops advertising, and a browser cannot connect by MAC address. To use the
  page, get the band advertising (drop the phone connection / put it in pairing),
  then pick it in the chooser.
- **MTU.** Web Bluetooth does not expose the negotiated ATT MTU, so the app assumes
  512 (what this band negotiates on Windows). If your stack uses a smaller MTU,
  large icon transfers will fail — see `link.js` (`DEFAULT_MTU`).
- **Not verified on hardware.** The protocol bytes are unit-tested against the
  Python reference; the actual connect/handshake/icon/media flows still need a real
  band.

## Run locally

```sh
# from this folder
python -m http.server 8080
# open http://localhost:8080   (secure-context, so Web Bluetooth is allowed)
```

## Deploy to GitHub Pages

A workflow (`.github/workflows/pages.yml`) publishes the `web/` folder.

1. Push this repo to GitHub.
2. **Settings → Pages → Source: GitHub Actions → Save**.
3. Push to `main`/`master` (or run the *Deploy web to GitHub Pages* action
   manually). The site is then live at
   `https://<you>.github.io/<repo>/` — make sure you open the **https** URL.

## Files

| File | Purpose |
|---|---|
| `index.html` | Single-page UI |
| `styles.css` | Styling |
| `js/sap.js` | SAP: CRC/frames, HELLO, session/ACK, message builders & parsers, weather + bandface (port of `sap.py`, `bandface.py`, `weather.py`) |
| `js/link.js` | Web Bluetooth transport + session state machine, reconnect |
| `js/icons.js` | Renders notification icons into the band's `[R][G][B][255-alpha]` raster |
| `js/features.js` | Routes decoded messages → battery/media/notify/watchface/weather behaviour |
| `js/app.js` | UI wiring, media host, composer, weather, watchfaces, logging |

The top-level `web_selftest.js` runs the protocol self-tests (`node web_selftest.js`).
