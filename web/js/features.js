/*
 * features.js — routes decoded SAP messages to feature logic. This is the browser
 * counterpart of server.py's _dispatch / _on_media / _on_notification / _on_weather.
 *
 * The link is transport-only; every message lands in `route(channel, body)`. What
 * we can do here is limited to what a web page is allowed to do (no Windows volume,
 * no OS toasts, no SendInput) — the rest is dropped gracefully and logged.
 */
"use strict";

(function () {
  const MEDIA_REQUEST_MIN_INTERVAL = 3000;  // ms; the band echoes our own pushes
  const WEATHER_MIN_INTERVAL = 5000;
  const BAND_MAX_VOLUME = 15;               // band uses Android's 0-15 scale

  class Features {
    constructor(link, deps) {
      this.link = link;
      this.deps = deps || {};               // media/battery/etc hooks from app
      this._bigdata = new SAP.BigData();
      this._mediaPushAt = 0;
      this._weatherPushAt = 0;
      this._log = deps.log || function () {};
      this._status = deps.status || function () {};
    }

    // ---- inbound entry -------------------------------------------------------
    async route(channel, body) {
      if (!body || !body.length) return;
      const msgId = body[0] & 0x3F;
      switch (channel) {
        case SAP.CH_MEDIA: return await this._onMedia(msgId, body);
        case SAP.CH_SETTINGS: return await this._onSettings(msgId, body);
        case SAP.CH_NOTIFICATION: return await this._onNotification(msgId, body);
        case SAP.CH_BANDFACE: return await this._onBandface(body);
        case SAP.CH_WEATHER: return await this._onWeather(msgId);
        default:
          this._log("ch=0x" + channel.toString(16).padStart(2, "0") +
            " msg=" + msgId + " body=" + SAP.hexSpace(body, 40));
      }
    }

    // ---- media ----------------------------------------------------------------
    async _onMedia(msgId, body) {
      const media = this.deps.media;
      // "send me state" — answer, but the band re-asks after our push so damp it.
      if (msgId === SAP.MSG_MEDIA_STATE_REQ_A || msgId === SAP.MSG_MEDIA_STATE_REQ_B) {
        if (Date.now() - this._mediaPushAt < MEDIA_REQUEST_MIN_INTERVAL) return;
        return this.pushMediaState("band asked");
      }
      if (msgId === SAP.MSG_MEDIA_CAPABILITY_REQ) {
        const max = (media && media.getMaxVolume) ? media.getMaxVolume() : BAND_MAX_VOLUME;
        await this.link.sendMessage(SAP.CH_MEDIA, SAP.buildMediaCapability(max, max));
        this._log("capability -> band: maxVolume=" + max);
        return;
      }
      if (msgId === SAP.MSG_MEDIA_VOLUME_REQ) {
        return this.pushVolume("band asked");
      }
      if (msgId === SAP.MSG_MEDIA_VOLUME) {
        const v = SAP.parseMediaVolume(body);
        if (v) {
          this._log("volume command: " + (SAP.VOL_CMD_NAMES[v.command] || ("cmd" + v.command)) +
            (v.command === SAP.VOL_SET ? " " + v.value : ""));
          if (media && media.volumeCommand) await media.volumeCommand(v.command, v.value);
        }
        return this.pushVolume("after change");
      }
      if (msgId === SAP.MSG_MEDIA_KEY) {
        const btn = SAP.parseMediaButton(body);
        if (btn) {
          if (btn.key_action === SAP.ACTION_PRESSED) {
            const name = SAP.MEDIA_KEY_NAMES[btn.key_code] || ("key" + btn.key_code);
            this._log("media button: " + name + " pressed");
            if (media && media.handleKey) await media.handleKey(btn.key_code);
            await new Promise(r => setTimeout(r, 400));
            await this.pushMediaState("after key");
          }
        }
        return;
      }
      this._log("media msg=" + msgId + " body=" + SAP.hexSpace(body, 40));
    }

    async pushMediaState(reason) {
      const media = this.deps.media || {};
      const st = media.getState ? media.getState() : {};
      this._mediaPushAt = Date.now();
      await this.link.sendMessage(SAP.CH_MEDIA, SAP.buildNowPlaying(
        st.title, st.artist, st.durationMs || 0, st.albumArt || "", st.appId || ""));
      await this.link.sendMessage(SAP.CH_MEDIA, SAP.buildPlaybackState(
        !!st.playing, st.positionMs || 0, 0, st.appId || ""));
      this._log("media -> band: " + (st.title || "(idle)") +
        (st.artist ? " — " + st.artist : "") + "  (" + reason + ")");
    }

    async pushVolume(reason) {
      const media = this.deps.media || {};
      let units = null;
      if (media.getVolumeUnits) units = media.getVolumeUnits();
      if (units === null || units === undefined) units = 0;
      await this.link.sendMessage(SAP.CH_MEDIA, SAP.buildMediaVolume(units));
      this._log("volume -> band: " + units + "/" + BAND_MAX_VOLUME + "  (" + reason + ")");
    }

    // ---- battery --------------------------------------------------------------
    async _onSettings(msgId, body) {
      if (msgId === SAP.MSG_BATTERY) {
        const info = SAP.parseBatteryResponse(body);
        if (info) {
          this._status("battery: " + info.level + "%" + (info.charging ? " (charging)" : ""));
          if (this.deps.battery) this.deps.battery(info.level, info.charging);
          return;
        }
      }
      this._log("settings msg=" + msgId + " body=" + SAP.hexSpace(body, 40));
    }

    // ---- notifications ----------------------------------------------------------
    async _onNotification(msgId, body) {
      if (msgId === SAP.MSG_NOTI_ICON) {
        const req = SAP.parseIconRequest(body);
        if (!req) {
          this._log("icon request unparsed: " + SAP.hexSpace(body, 60));
          return;
        }
        const url = req.url, size = req.size;
        const appId = url.replace(/^APP_ICON_/, "");
        const bank = this.deps.icons;
        let pixels;
        if (bank) {
          pixels = bank.pixelsFor(appId, url, size);
        } else {
          pixels = new Uint8Array(size * size * 4);
        }
        const resp = SAP.buildIconResponse(url, size, pixels);
        const cap = Math.min(SAP.BIGDATA_CAP, this.link.mtu - 3 - 6 - 4);
        const chunks = this._bigdata.chunk(resp, cap);
        this._log("icon <- band: " + url + " size=" + size +
          " (" + pixels.length + " px bytes -> " + chunks.length + " chunk(s))");
        for (let i = 0; i < chunks.length; i++) {
          if (!await this.link.sendMessage(SAP.CH_NOTIFICATION, chunks[i])) {
            this._log("icon transfer ABORTED at chunk " + (i + 1) + "/" + chunks.length);
            return;
          }
        }
        this._log("icon sent: " + chunks.length + " chunks");
        return;
      }

      if (msgId === SAP.MSG_NOTI_CLEAR) {
        const seq = SAP.parseNotificationClear(body);
        if (seq === null || seq === undefined) {
          this._log("clear (unparsed): " + SAP.hexSpace(body, 40));
          return;
        }
        this._log("band dismissed notification #" + seq);
        if (this.deps.onNotifyClear) this.deps.onNotifyClear(seq);
        return;
      }

      if (msgId === SAP.MSG_LARGE_DATA_ACK) {
        const ack = SAP.parseBigdataAck(body);
        const idx = body.length >= 6 ? SAP.readU32LE(body, 2) : null;
        this._log("big-data ack: version=" + (ack ? ack.version : "?") +
          " success=" + (ack ? ack.success : "?") +
          (idx !== null ? " chunk_idx=" + idx : ""));
        return;
      }

      if (msgId === SAP.MSG_NOTI_ACTION) {
        const act = SAP.parseNotificationAction(body);
        if (act && this.deps.onNotifyAction) this.deps.onNotifyAction(act.seq, act.text);
        else if (act) this._log("quick reply: seq=" + act.seq + " text=" + act.text);
        return;
      }

      this._log("noti msg=" + msgId + " body=" + SAP.hexSpace(body, 40));
    }

    // ---- watchfaces -------------------------------------------------------------
    async _onBandface(body) {
      const parsed = SAP.bandface.parse(body);
      this._log("BANDFACE <- " + describeBandface(parsed));
      if (this.deps.onBandface) this.deps.onBandface(parsed);
    }

    // ---- weather -----------------------------------------------------------------
    async _onWeather(msgId) {
      if (msgId === SAP.MSG_WEATHER_INFO || msgId === SAP.MSG_WEATHER_ADD_LOCATION) {
        if (Date.now() - this._weatherPushAt < WEATHER_MIN_INTERVAL) return;
        if (this.deps.pushWeather) await this.deps.pushWeather();
        return;
      }
      this._log("weather msg=" + msgId);
    }

    async pushWeatherToBand(w) {
      this._weatherPushAt = Date.now();
      const body = SAP.buildWeather(w, false, SAP.uvLabel(w.uv));
      await this.link.sendMessage(SAP.CH_WEATHER, body);
      return body;
    }
  }

  function describeBandface(d) {
    if (!d.msg) return "(empty) raw=" + (d.raw || "");
    const scalars = [];
    for (const k in d) {
      if (k === "raw" || k === "msg" || k === "msg_id" || k === "is_response" ||
          k === "watchfaces") continue;
      scalars.push(k + "=" + d[k]);
    }
    let head = d.msg + (d.is_response ? "(resp)" : "(req)") +
      (scalars.length ? "  " + scalars.join("  ") : "");
    const lines = [head];
    if (d.watchfaces) {
      for (const wf of d.watchfaces) {
        lines.push("    id=" + wf.WF_ID + " pos=" + wf.POSITION_INDEX +
          " style=" + wf.STYLE_ID + "/" + wf.STYLE_INDEX +
          " ver=" + wf.WF_VERSION + " name=" + wf.WF_NAME +
          (wf.IS_CURRENT ? "  <-- CURRENT" : ""));
      }
    }
    return lines.join("\n");
  }

  window.GF3Features = { Features, BAND_MAX_VOLUME };
})();
