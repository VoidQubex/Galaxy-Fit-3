/*
 * link.js — Web Bluetooth transport + SAP session for the Galaxy Fit 3.
 *
 * Rides the two raw GATT characteristics of the Samsung SAP service and does the
 * whole SAP session layer in the browser:
 *   service 00001a1a, notify 797ae4e9-... (band->phone), write 63e30bad-... (phone->band)
 *
 * Sequence/ACK, outbound fragmentation and the HELLO handshake live here. It is
 * deliberately transport-only: after the handshake it hands every decoded message
 * to `opts.onMessage(channel, body)` so features/app can decide what to do.
 */
"use strict";

(function () {
  const SVC_SAP = "00001a1a-0000-1000-8000-00805f9b34fb";
  const CH_NOTIFY = "797ae4e9-2e58-4fe8-b48d-b5c79599fb9b";
  const CH_WRITE = "63e30bad-4206-4596-839f-e47cbf7a4b5d";

  const CH_CONTROL = 0x01;
  const PHONE_MODEL = "M2007J3SY";
  const PHONE_VENDOR = "Xiaomi";
  const DEFAULT_MTU = 512;   // negotiated with this band; Web Bluetooth hides it

  function bufToArray(v) {
    if (v instanceof Uint8Array) return v;
    if (typeof Buffer !== "undefined" && Buffer.isBuffer(v)) return new Uint8Array(v);
    if (v.buffer instanceof ArrayBuffer) return new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
    return new Uint8Array(v);
  }

  class Link {
    constructor(opts) {
      opts = opts || {};
      this.opts = opts;
      this.mtu = opts.mtu || DEFAULT_MTU;
      this.phoneModel = opts.phoneModel || PHONE_MODEL;
      this.phoneVendor = opts.phoneVendor || PHONE_VENDOR;

      this.device = null;
      this.server = null;
      this.service = null;
      this.notifyChar = null;
      this.writeChar = null;

      this.seq = 0x40;
      this.handshakeDone = false;
      this._busy = false;
      this._writeChain = Promise.resolve();
      this._writeFast = false;   // pending flag applies to whole sendMessage batch
      this._inChain = Promise.resolve();   // serializes inbound message handling

      this._log = opts.onLog || function () {};
      this._status = opts.onStatus || function () {};
    }

    // --- small public helpers ------------------------------------------------
    log(msg) { this._log(msg); }
    status(msg) { this._status(msg); }
    raw(msg) { this._log(msg); }

    nextSeq() {
      const s = this.seq;
      this.seq = (this.seq + 0x10) & 0xFF;
      if (this.seq === 0x00) this.seq = 0x40;
      return s;
    }

    // Enqueue a single raw write so frames never interleave. Returns a promise
    // that resolves to true on success.
    _writeRaw(frame) {
      const op = this._writeChain.then(() => this._doWrite(frame));
      // Keep the chain alive regardless of individual failures.
      this._writeChain = op.catch(() => {});
      return op;
    }

    async _doWrite(frame) {
      if (!this.writeChar) {
        this._log("write: no characteristic (disconnected?)");
        return false;
      }
      try {
        await this.writeChar.writeValueWithResponse(SAP.toBytes(frame));
        return true;
      } catch (e) {
        this._log("WRITE FAILED (" + frame.length + "B): " + (e && e.message ? e.message : e));
        return false;
      }
    }

    // Send `body` on `channel`, fragmenting to fit the MTU. Returns true if every
    // frame was accepted by the transport.
    async sendMessage(channel, body, fast) {
      let frames;
      try {
        frames = SAP.buildFrames(channel, this.nextSeq(), SAP.toBytes(body), this.mtu);
      } catch (e) {
        this._log("sendMessage error: " + e.message);
        return false;
      }
      for (const f of frames) {
        if (!await this._writeRaw(f)) return false;
      }
      return true;
    }

    async sendRaw(frame) {
      return this._writeRaw(SAP.toBytes(frame));
    }

    sendAck(seq, chunk) {
      return this.sendRaw(SAP.frameWrap(SAP.packetEncodeAck(seq, chunk)));
    }

    // --- Bluetooth plumbing ----------------------------------------------------
    static supported() {
      return typeof navigator !== "undefined" && !!navigator.bluetooth;
    }

    // Returns a device picked through the chooser. `extraFilters` may add more
    // requestDevice filters (e.g. by name).
    async pickDevice(extraFilters) {
      const base = { filters: [{ services: [SVC_SAP] }] };
      if (extraFilters && extraFilters.length) base.filters = base.filters.concat(extraFilters);
      this.device = await navigator.bluetooth.requestDevice(base);
      return this.device;
    }

    async open() {
      const device = this.device;
      if (!device) throw new Error("No device selected");
      this._log("connecting to " + (device.name || device.id) + " ...");
      device.addEventListener("gattserverdisconnected", () => this._onDisconnected());
      this.server = await device.gatt.connect();
      this.service = await this.server.getPrimaryService(SVC_SAP);
      const chars = await this.service.getCharacteristics();
      for (const c of chars) {
        const u = c.uuid.toLowerCase();
        if (u.indexOf("797ae4e9") === 0) this.notifyChar = c;
        else if (u.indexOf("63e30bad") === 0) this.writeChar = c;
      }
      if (!this.notifyChar || !this.writeChar) {
        throw new Error("SAP characteristics (1a1a) not found on this device");
      }
      await this.notifyChar.startNotifications();
      this.notifyChar.addEventListener("characteristicvaluechanged", (e) =>
        this._onNotify(bufToArray(e.target.value)));
      this.status("connected & subscribed — waiting for the band HELLO…");
      this._log("MTU assumed " + this.mtu + " (Web Bluetooth does not expose the negotiated value)");
    }

    async connect() {
      await this.pickDevice(this.opts.extraFilters);
      await this.open();
      // Wait (with timeout) for the handshake the band drives.
      await this._awaitHandshake();
    }

    _onDisconnected() {
      this.handshakeDone = false;
      this.server = null; this.service = null; this.notifyChar = null; this.writeChar = null;
      this.status("link dropped");
      if (this.opts.onDisconnect) this.opts.onDisconnect();
    }

    async disconnect() {
      try {
        if (this.notifyChar) await this.notifyChar.stopNotifications();
      } catch (e) { /* ignore */ }
      try {
        if (this.device && this.device.gatt.connected) this.device.gatt.disconnect();
      } catch (e) { /* ignore */ }
    }

    async _onNotify(data) {
      // Process one GATT notification (one SAP frame) at a time so responses we
      // send mid-handling can't interleave with the next frame's handler.
      const run = this._inChain.then(() => this._handleFrame(data));
      this._inChain = run.catch((e) => this._log("inbound error: " + (e && e.message ? e.message : e)));
      return run;
    }

    async _handleFrame(frame) {
      if (!frame || !frame.length) return;
      if (SAP.frameIsHello(frame)) {
        if (SAP.helloIsBand(frame)) await this._onBandHello(frame);
        return;
      }
      const payload = SAP.frameUnwrap(frame);
      if (payload === null) {
        if (this.opts.onRawFrame) this.opts.onRawFrame(frame, "crc/length fail");
        return;
      }
      const pkt = SAP.packetParse(payload);
      if (pkt.kind === "ack") return;
      if (pkt.kind === "single" || pkt.kind === "first") {
        // Acknowledge then dispatch. Ack is sent first through the same queue so
        // ordering against our own writes stays sane.
        await this.sendAck(pkt.seq, pkt.kind === "first");
        if (this.opts.onMessage) this.opts.onMessage(pkt.channel, pkt.body, pkt.kind);
      } else if (pkt.kind === "cont") {
        await this.sendAck(pkt.seq, true);
      }
      if (this.opts.onPacket) this.opts.onPacket(pkt);
    }

    async _onBandHello(frame) {
      const hello = SAP.parseBandHello(frame);
      if (hello === null) return;
      this._log("band HELLO: model='" + hello.model + "' vendor='" + hello.vendor + "'");
      this.status("band handshaking…");
      // Reply HELLO, then the two control messages the band needs before it will
      // service other channels.
      await this.sendRaw(SAP.buildPhoneHello(
        hello, SAP.randomToken(), this.phoneModel, this.phoneVendor, null));
      await this.sendMessage(CH_CONTROL, SAP.buildInitSetting());
      await this.sendMessage(CH_CONTROL, SAP.buildLicenseRequest(true));
      this.handshakeDone = true;
      this._handshakeResolve && this._handshakeResolve();
      this.status("handshake complete");
      if (this.opts.onHandshake) this.opts.onHandshake(hello);
    }

    _awaitHandshake() {
      return new Promise((resolve, reject) => {
        if (this.handshakeDone) return resolve();
        const timer = setTimeout(() => {
          if (this.handshakeDone) resolve();
          else reject(new Error("No band HELLO within 15s — is it connected to a phone, or asleep?"));
        }, 15000);
        this._handshakeResolve = () => { clearTimeout(timer); resolve(); };
      });
    }
  }

  window.GF3Link = { Link, SVC_SAP, CH_NOTIFY, CH_WRITE };
})();
