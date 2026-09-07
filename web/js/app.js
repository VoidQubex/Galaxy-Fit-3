/*
 * app.js — UI wiring for the Galaxy Fit 3 web page.
 *
 * Owns the browser-side "host": the Web Bluetooth link, the media host that the band
 * remote drives, the notification composer (with icon serving + quick replies), the
 * weather fetcher and the watchface list/switcher. OS-only features (Windows volume,
 * toasts, SendInput) have no browser equivalent and are omitted here on purpose.
 */
"use strict";

(function () {
  function $(id) { return document.getElementById(id); }

  // ------------------------------------------------------------------ helpers
  const ts = () => new Date().toTimeString().slice(0, 8);
  const pad = (n) => String(n).padStart(2, "0");
  function fmtMs(ms) {
    const s = Math.max(0, Math.floor((ms || 0) / 1000));
    return pad(Math.floor(s / 60)) + ":" + pad(s % 60);
  }
  function addLog(text, cls) {
    const el = $("log");
    const line = document.createElement("div");
    if (cls) line.className = cls;
    const t = document.createElement("span"); t.className = "t"; t.textContent = "[" + ts() + "] ";
    line.appendChild(t);
    line.appendChild(document.createTextNode(text));
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }

  let connected = false;
  let handshaken = false;
  let link = null;
  let features = null;
  let deviceMeta = { model: null };
  let battery = null;

  const ENABLE_IDS = [
    "btnRefreshBattery", "btnSendNoti", "btnPushNowPlaying",
    "btnListFaces", "btnActiveFace", "btnSendWeather",
  ];
  function setArmed() {
    const armed = connected && handshaken;
    for (const id of ENABLE_IDS) $(id).disabled = !armed;
  }

  function setPill(state, text) {
    const p = $("connPill");
    p.className = "pill pill-" + state;
    p.textContent = text;
  }
  function setMeta() {
    $("metaConn").textContent = connected ? "connected" : "disconnected";
    $("metaHandshake").textContent = handshaken ? "done" : (connected ? "waiting…" : "—");
    $("metaModel").textContent = deviceMeta.model || "—";
  }

  // ------------------------------------------------------------------- logging
  $("btnClearLog").addEventListener("click", () => { $("log").textContent = ""; });

  // ------------------------------------------------------------------ media host
  const mediaHost = {
    playing: false,
    simPos: 0,
    simDuration: 210,       // seconds when no real audio is loaded
    simTimer: null,
    audio: $("audioEl"),
    srcUrl: null,

    hasAudio() { return !!this.srcUrl; },

    currentState() {
      let dur = this.simDuration * 1000, pos = this.simPos * 1000;
      if (this.hasAudio() && !isNaN(this.audio.duration)) {
        dur = this.audio.duration * 1000;
        pos = this.audio.currentTime * 1000;
      }
      return {
        title: $("mediaTitle").value || "",
        artist: $("mediaArtist").value || "",
        durationMs: Math.round(dur),
        positionMs: Math.round(pos),
        playing: this.playing,
        albumArt: "",
        appId: "web.media",
      };
    },

    syncFromAudio() {
      if (this.hasAudio()) this.playing = !this.audio.paused && !this.audio.ended;
      this.renderMini();
    },

    renderMini() {
      const st = this.currentState();
      $("mediaStateTag").textContent = st.playing ? "playing" : (st.positionMs > 500 ? "paused" : "stopped");
      $("mediaPos").textContent = fmtMs(st.positionMs) + " / " + fmtMs(st.durationMs);
    },

    async togglePlay() {
      if (this.hasAudio()) {
        if (this.audio.paused) { try { await this.audio.play(); } catch (e) { addLog("play() blocked: " + (e && e.message), "err"); } }
        else this.audio.pause();
      } else {
        this.playing = !this.playing;
        if (this.playing) {
          if (this.simPos >= this.simDuration) this.simPos = 0;
          this._startSim();
        } else {
          this._stopSim();
        }
      }
      this.syncFromAudio();
      if (features && handshaken) await features.pushMediaState("toggle");
      this.updateMediaSession();
    },

    _startSim() {
      this._stopSim();
      this.simTimer = setInterval(() => {
        this.simPos += 1;
        if (this.simPos >= this.simDuration) { this.simPos = 0; this.playing = false; this._stopSim(); }
        this.renderMini();
      }, 1000);
    },
    _stopSim() { if (this.simTimer) { clearInterval(this.simTimer); this.simTimer = null; } },

    async key(code) {
      if (code === SAP.KEY_PLAYPAUSE) return this.togglePlay();
      if (code === SAP.KEY_PREVIOUS) {
        if (this.hasAudio()) this.audio.currentTime = 0;
        else this.simPos = 0;
        this.renderMini();
      } else if (code === SAP.KEY_NEXT) {
        addLog("media next: no playlist defined (ignored)", "dim");
      } else if (code === SAP.KEY_STOP) {
        this.playing = false;
        if (this.hasAudio()) { this.audio.pause(); this.audio.currentTime = 0; }
        this.simPos = 0;
        this._stopSim();
        this.renderMini();
      }
      if (features && handshaken) await features.pushMediaState("key");
    },

    async volumeCommand(cmd, value) {
      const slider = $("mediaVol");
      let cur = parseInt(slider.value, 10) || 0;
      if (cmd === SAP.VOL_SET) cur = value;
      else if (cmd === SAP.VOL_UP) cur = Math.min(15, cur + 1);
      else if (cmd === SAP.VOL_DOWN) cur = Math.max(0, cur - 1);
      slider.value = cur;
      if (this.audio) this.audio.volume = cur / 15;
      this.renderMini();
    },

    setVolumeUnits(u) {
      const v = Math.max(0, Math.min(15, Math.round(u || 0)));
      $("mediaVol").value = v;
      if (this.audio) this.audio.volume = v / 15;
    },
    getVolumeUnits() { return parseInt($("mediaVol").value, 10) || 0; },
    getMaxVolume() { return 15; },

    async loadSrc(url) {
      if (this.srcUrl) URL.revokeObjectURL(this.srcUrl);
      this.srcUrl = url || null;
      if (!url) { this.audio.removeAttribute("src"); this.audio.load(); return; }
      this.audio.src = url;
      this.audio.load();
      // try to autoplay to give real progress
      this.playing = false;
      this.audio.addEventListener("loadedmetadata", () => { this.renderMini(); }, { once: true });
    },

    updateMediaSession() {
      if (!("mediaSession" in navigator)) return;
      try {
        navigator.mediaSession.metadata = new MediaMetadata({
          title: $("mediaTitle").value || "Galaxy Fit 3",
          artist: $("mediaArtist").value || "",
          album: "Web media",
        });
        navigator.mediaSession.setActionHandler("play", () => mediaHost.togglePlay());
        navigator.mediaSession.setActionHandler("pause", () => mediaHost.togglePlay());
        navigator.mediaSession.setActionHandler("previoustrack", () => mediaHost.key(SAP.KEY_PREVIOUS));
        navigator.mediaSession.setActionHandler("nexttrack", () => mediaHost.key(SAP.KEY_NEXT));
      } catch (e) { /* optional */ }
    },
  };

  // Keep position/pause display fresh and nudge the band's media screen while playing.
  ["play", "pause", "timeupdate", "ended"].forEach((ev) =>
    $("audioEl").addEventListener(ev, () => mediaHost.syncFromAudio()));
  setInterval(() => {
    if (!(handshaken && features && mediaHost.playing)) return;
    features.pushMediaState("tick");
  }, 5000);

  // page media controls
  $("btnPlayPause").addEventListener("click", () => mediaHost.togglePlay());
  $("btnPrev").addEventListener("click", () => mediaHost.key(SAP.KEY_PREVIOUS));
  $("btnNext").addEventListener("click", () => mediaHost.key(SAP.KEY_NEXT));
  $("btnPushNowPlaying").addEventListener("click", async () => {
    if (features && handshaken) await features.pushMediaState("manual");
  });
  $("mediaVol").addEventListener("input", () => {
    if (mediaHost.audio) mediaHost.audio.volume = (parseInt($("mediaVol").value, 10) || 0) / 15;
    if (features && handshaken) features.pushVolume("slider");
  });
  $("mediaUrl").addEventListener("change", () => { if ($("mediaUrl").value) mediaHost.loadSrc($("mediaUrl").value.trim()); });
  $("mediaFile").addEventListener("change", () => {
    const f = $("mediaFile").files[0];
    if (!f) return;
    $("mediaUrl").value = "";
    mediaHost.loadSrc(URL.createObjectURL(f));
    addLog("loaded audio: " + f.name);
  });
  ["mediaTitle", "mediaArtist"].forEach((id) =>
    $(id).addEventListener("input", () => mediaHost.updateMediaSession()));

  // ------------------------------------------------------------- notification UI
  const notiState = { seq: 0, image: null };
  $("notiIcon").addEventListener("change", () => {
    const f = $("notiIcon").files[0];
    if (!f) return;
    const url = URL.createObjectURL(f);
    const img = new Image();
    img.onload = () => {
      notiState.image = img;
      const pre = $("notiIconPreview");
      pre.classList.remove("empty");
      pre.innerHTML = "";
      pre.appendChild(img);
    };
    img.src = url;
  });
  function quickReplyLabels() {
    return $("quickReplies").value.split(",").map((s) => s.trim()).filter(Boolean);
  }

  async function sendNotification(extra) {
    if (!(link && handshaken)) return;
    const title = $("notiTitle").value;
    const appName = $("notiApp").value || "Web";
    if (!title) { addLog("notification needs a title", "err"); return; }
    notiState.seq += 1;
    const seq = notiState.seq;
    const pkg = "web.notify." + appName.toLowerCase().replace(/[^a-z0-9]+/g, "_");

    // Register the icon to serve when the band asks for it.
    const iconBank = iconBankObj;
    iconBank.set(SAP.appIdFor(pkg), appName, notiState.image);

    // Quick replies: switch the feature on and set the phrase list right before
    // sending, so it only applies to this notification.
    const wantQr = $("notiQuickReply").checked;
    if (wantQr) {
      const labels = quickReplyLabels();
      await link.sendMessage(SAP.CH_QUICK_REPLY, SAP.buildQuickReplySwitch(true));
      if (labels.length) await link.sendMessage(SAP.CH_QUICK_REPLY, SAP.buildQuickReplyList(labels));
    }

    const payload = SAP.buildNotification({
      seq,
      title,
      body: $("notiBody").value,
      app_name: appName,
      pkg,
      popup: $("notiPopup").checked,
      need_quick_reply: wantQr,
      alert_type: $("notiAlert").checked ? SAP.ALERT_VIBRATION_ONLY : SAP.ALERT_SILENT,
      icon_newly_added: true,   // always re-fetch the icon we now serve
    });
    await link.sendMessage(SAP.CH_NOTIFICATION, payload);
    addLog("notify -> band: " + title + " / " + $("notiBody").value + "  (" + appName + ")");
  }
  $("btnSendNoti").addEventListener("click", sendNotification);

  function appendReply(seq, text) {
    const box = document.createElement("div");
    box.className = "reply";
    box.textContent = "reply on band (seq " + seq + "): " + text;
    $("replyLog").prepend(box);
    // Optional: surface it as a desktop/browser notification too.
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification("Galaxy Fit 3 reply", { body: text }); } catch (e) { /* ignore */ }
    }
  }

  // ---------------------------------------------------------------- icon bank
  const iconBankObj = new GF3Icons.IconBank();

  // ----------------------------------------------------------------- watchfaces
  let facesData = { watchfaces: null, current: null };
  function renderFaces() {
    const ul = $("faceList");
    ul.innerHTML = "";
    const faces = facesData.watchfaces;
    if (!faces || !faces.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = faces && !faces.length ? "no watchfaces listed" : "press “List faces”";
      ul.appendChild(li);
      return;
    }
    const current = facesData.current;
    for (const wf of faces) {
      const li = document.createElement("li");
      if (current !== undefined && wf.WF_ID === current) li.classList.add("current");
      const idx = document.createElement("span");
      idx.className = "face-idx";
      idx.textContent = "#" + wf.WF_ID;
      const name = document.createElement("span");
      name.className = "face-name";
      name.textContent = wf.WF_NAME || ("face " + wf.WF_ID);
      const meta = document.createElement("span");
      meta.className = "face-meta";
      meta.textContent = (wf.IS_CURRENT ? "● active" : "tap to activate") + (wf.IS_EDITABLE ? " · editable" : "");
      li.appendChild(idx); li.appendChild(name); li.appendChild(meta);
      li.addEventListener("click", async () => {
        if (wf.IS_CURRENT) return;
        $("faceStatus").textContent = "switching to #" + wf.WF_ID + "…";
        addLog("BANDFACE -> SET_CURRENT_BANDFACE id=" + wf.WF_ID);
        await link.sendMessage(SAP.CH_BANDFACE, SAP.bandface.builders.setCurrentBandface(wf.WF_ID));
        setTimeout(refreshFaces, 1500);
      });
      ul.appendChild(li);
    }
  }
  async function refreshFaces() {
    if (!(link && handshaken)) return;
    $("faceStatus").textContent = "querying…";
    await link.sendMessage(SAP.CH_BANDFACE, SAP.bandface.builders.getAllInfo());
    setTimeout(async () => {
      await link.sendMessage(SAP.CH_BANDFACE, SAP.bandface.builders.getCurrentBandface());
    }, 600);
  }
  $("btnListFaces").addEventListener("click", refreshFaces);
  $("btnActiveFace").addEventListener("click", async () => {
    await link.sendMessage(SAP.CH_BANDFACE, SAP.bandface.builders.getCurrentBandface());
  });

  // ------------------------------------------------------------------ weather
  let lastWx = null;
  function isoToEpoch(s) {
    const t = new Date(s).getTime();
    return isNaN(t) ? Math.floor(Date.now() / 1000) : Math.floor(t / 1000);
  }
  async function buildWeatherFromApi(lat, lon) {
    const url = "https://api.open-meteo.com/v1/forecast" +
      "?latitude=" + lat + "&longitude=" + lon +
      "&current=temperature_2m,apparent_temperature,relative_humidity_2m," +
      "weather_code,is_day,wind_speed_10m,wind_direction_10m" +
      "&hourly=temperature_2m,weather_code,precipitation_probability,is_day" +
      "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max" +
      "&timezone=auto&forecast_days=8";
    const r = await fetch(url);
    if (!r.ok) throw new Error("Open-Meteo HTTP " + r.status);
    const d = await r.json();
    const cur = d.current, daily = d.daily, hourly = d.hourly;
    const now = Math.floor(Date.now() / 1000);
    const times = hourly.time.map(isoToEpoch);
    let start = 0;
    for (let i = 0; i < times.length; i++) { if (times[i] >= now) { start = i; break; } }
    const hours = [];
    for (let i = start; i < Math.min(start + 6, times.length); i++) {
      hours.push({
        t: times[i], temp: Math.round(hourly.temperature_2m[i]),
        icon: SAP.wmoToSamsungIcon(hourly.weather_code[i]),
        precip: hourly.precipitation_probability[i] || 0,
        is_day: hourly.is_day[i],
      });
    }
    const days = [];
    for (let i = 0; i < Math.min(4, daily.time.length); i++) {
      days.push({
        t: isoToEpoch(daily.time[i] + "T12:00"),
        icon: SAP.wmoToSamsungIcon(daily.weather_code[i]),
        max: Math.round(daily.temperature_2m_max[i]),
        min: Math.round(daily.temperature_2m_min[i]),
      });
    }
    return {
      city: $("wxCity").value || "My area", region: "", country: "",
      time: now,
      temp: Math.round(cur.temperature_2m),
      feels_like: Math.round(cur.apparent_temperature),
      humidity: Math.round(cur.relative_humidity_2m),
      icon: SAP.wmoToSamsungIcon(cur.weather_code),
      is_day: cur.is_day,
      wind_speed: Math.round(cur.wind_speed_10m),
      wind_dir: SAP.compass(cur.wind_direction_10m),
      high: Math.round(daily.temperature_2m_max[0]),
      low: Math.round(daily.temperature_2m_min[0]),
      sunrise: isoToEpoch(daily.sunrise[0]),
      sunset: isoToEpoch(daily.sunset[0]),
      uv: Math.round((daily.uv_index_max || [0])[0] || 0),
      hourly: hours, daily: days,
    };
  }
  async function pushWeather() {
    if (!(link && handshaken)) return;
    let lat = parseFloat($("wxLat").value), lon = parseFloat($("wxLon").value);
    if (isNaN(lat) || isNaN(lon)) { lat = 51.5072; lon = -0.1276; }
    $("wxStatus").textContent = "fetching…";
    try {
      lastWx = await buildWeatherFromApi(lat, lon);
      const body = await features.pushWeatherToBand(lastWx);
      $("wxStatus").textContent =
        lastWx.city + ": " + lastWx.temp + "C (" + lastWx.low + "-" + lastWx.high + "C), " +
        lastWx.hourly.length + "h/" + lastWx.daily.length + "d  (" + body.length + "B)";
      addLog("weather -> band (" + body.length + "B) " + lastWx.city + " " + lastWx.temp + "C", "ok");
    } catch (e) {
      $("wxStatus").textContent = "weather failed: " + (e && e.message ? e.message : e);
      addLog("weather fetch failed: " + (e && e.message ? e.message : e), "err");
    }
  }
  $("btnSendWeather").addEventListener("click", pushWeather);
  $("btnWxLocate").addEventListener("click", () => {
    if (!("geolocation" in navigator)) { $("wxStatus").textContent = "geolocation unavailable"; return; }
    navigator.geolocation.getCurrentPosition((p) => {
      $("wxLat").value = p.coords.latitude.toFixed(4);
      $("wxLon").value = p.coords.longitude.toFixed(4);
      $("wxStatus").textContent = "location set (" + p.coords.latitude.toFixed(2) + "," +
        p.coords.longitude.toFixed(2) + ")";
    }, (err) => {
      $("wxStatus").textContent = "location denied — enter coordinates manually";
    });
  });


  // ---------------------------------------------------------------- battery
  async function refreshBattery() {
    if (!(link && handshaken)) return;
    await link.sendMessage(SAP.CH_SETTINGS, SAP.buildBatteryRequest());
  }
  $("btnRefreshBattery").addEventListener("click", refreshBattery);
  setInterval(() => {
    if (connected && handshaken && $("autoPoll").checked) refreshBattery();
  }, 10000);

  function showBattery(level, charging) {
    battery = { level, charging };
    $("batteryLabel").textContent = (level < 0 ? "—" : level + "%");
    $("chargingLabel").textContent = charging ? "charging" : "";
    $("batteryFill").style.width = (level < 0 ? 0 : level) + "%";
    $("batteryFill").classList.toggle("low", level >= 0 && level <= 20);
  }

  // ------------------------------------------------------------------ connect
  function supported() {
    return GF3Link.Link.supported() && window.isSecureContext !== false;
  }

  async function doConnect() {
    if (!supported()) {
      $("supportBanner").classList.remove("hidden");
      return;
    }
    $("supportBanner").classList.add("hidden");
    $("btnConnect").disabled = true;
    setPill("scan", "scanning…");
    try {
      link = new GF3Link.Link({
        onLog: (m) => addLog(m),
        onStatus: (m) => addLog(m, "ok"),
        onMessage: (channel, body) => { if (features) features.route(channel, body); },
        onDisconnect: onDrop,
        onHandshake: onHandshake,
        extraFilters: [
          { namePrefix: "Galaxy Fit 3" },
          { namePrefix: "Galaxy" },
          { namePrefix: "SM-R39" },
        ],
      });
      features = new GF3Features.Features(link, {
        media: mediaHost,
        icons: iconBankObj,
        log: (m) => addLog(m),
        status: (m) => addLog(m, "ok"),
        battery: showBattery,
        onNotifyClear: (seq) => addLog("band cleared notification #" + seq),
        onNotifyAction: (seq, text) => appendReply(seq, text),
        onBandface: (parsed) => {
          if (parsed.watchfaces) facesData.watchfaces = parsed.watchfaces;
          if (parsed.CURRENT_WF_ID !== undefined) facesData.current = parsed.CURRENT_WF_ID;
          if (parsed.msg_id === 2 && parsed.WF_ID !== undefined) facesData.current = parsed.WF_ID;
          $("faceStatus").textContent = parsed.msg + "  " +
            (facesData.watchfaces ? facesData.watchfaces.length + " face(s)" : "") +
            (facesData.current !== null ? "  · current #" + facesData.current : "");
          renderFaces();
        },
        pushWeather: pushWeather,
      });

      setPill("connecting", "connecting…");
      await link.connect();   // pick + connect + handshake (throws if no HELLO)
      connected = true;
      // note: handshake sets handshaken via onHandshake
    } catch (e) {
      console.error(e);
      setPill("err", "error");
      addLog("connect error: " + (e && e.message ? e.message : e), "err");
      connected = false; handshaken = false;
      setArmed(); setMeta();
    } finally {
      $("btnConnect").disabled = false;
      setArmed();
    }
  }
  $("btnConnect").addEventListener("click", doConnect);

  async function onHandshake(hello) {
    handshaken = true;
    deviceMeta.model = hello.model;
    setPill("online", "online");
    setMeta(); setArmed();
    addLog("connected — priming features", "ok");
    // Poll battery + announce our capability/volume once so the band has data.
    await refreshBattery();
    const maxV = mediaHost.getMaxVolume();
    await link.sendMessage(SAP.CH_MEDIA, SAP.buildMediaCapability(maxV, maxV));
    await features.pushVolume("initial");
    await features.pushMediaState("initial");
  }

  function onDrop() {
    connected = false; handshaken = false; deviceMeta.model = null;
    setPill("err", "offline");
    setMeta(); setArmed();
    addLog("link dropped — press Connect to re-pair", "err");
  }

  $("btnDisconnect").addEventListener("click", async () => {
    if (link) await link.disconnect();
    onDrop();
    addLog("disconnected by user");
  });

  // ------------------------------------------------------------------ startup
  function init() {
    if (!("Notification" in window) || Notification.permission !== "denied") {
      // don't auto-ask; only prompt when first quick reply is enabled.
    }
    setMeta(); setPill("idle", "offline"); setArmed();
    if (!GF3Link.Link.supported() || window.isSecureContext === false) {
      $("supportBanner").classList.remove("hidden");
      $("connectNote").textContent = "";
    }
    renderFaces();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
