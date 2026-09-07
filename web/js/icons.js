/*
 * icons.js — draw the band's notification icons onto a <canvas> and export the
 * raster in the format the band expects.
 *
 * The band wants raw pixels: size x size, 4 bytes per pixel, [R][G][B][255-alpha].
 * Note the alpha byte is INVERTED versus the usual sense: 0 = opaque,
 * 255 = fully transparent (confirmed from the companion app, not guessed).
 */
"use strict";

(function () {
  function stableHash(s) {
    let h = 2166136261;
    s = String(s || "");
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function colorFor(key) {
    // Pleasant, stable colour derived from a package/app name.
    const h = (stableHash(key) & 0xFFFF) / 0xFFFF;
    // HSV -> RGB, s=0.62 v=0.88
    const c = hsv2rgb(h, 0.62, 0.88);
    return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
  }

  function hsv2rgb(h, s, v) {
    const i = Math.floor(h * 6);
    const f = h * 6 - i;
    const p = v * (1 - s);
    const q = v * (1 - f * s);
    const t = v * (1 - (1 - f) * s);
    const r = [v, q, p, p, t, v][i % 6];
    const g = [t, v, v, q, p, p][i % 6];
    const b = [p, p, t, v, v, q][i % 6];
    return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
  }

  function roundedRectPath(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // Convert a source ImageData into the band raster (byte 3 = 255 - alpha).
  function toBandPixels(imageData, size) {
    const out = new Uint8Array(size * size * 4);
    const d = imageData.data;
    let o = 0;
    for (let i = 0; i < size * size; i++) {
      out[o] = d[i * 4];
      out[o + 1] = d[i * 4 + 1];
      out[o + 2] = d[i * 4 + 2];
      out[o + 3] = 255 - d[i * 4 + 3];
      o += 4;
    }
    return out;
  }

  function makeCanvas(size) {
    const c = document.createElement("canvas");
    c.width = c.height = size;
    return c;
  }

  // A rounded letter tile in the colour of `key` — stand-in for a real app icon.
  function drawLetterTile(canvas, label, key) {
    const ctx = canvas.getContext("2d");
    const size = canvas.width;
    ctx.clearRect(0, 0, size, size);
    const radius = Math.max(2, Math.round(size / 5));
    ctx.fillStyle = colorFor(key === undefined ? label : key);
    roundedRectPath(ctx, 0, 0, size, size, radius);
    ctx.fill();
    const letter = (String(label || "").trim().charAt(0) || "?").toUpperCase();
    const px = Math.max(8, Math.round(size * 0.58));
    ctx.fillStyle = "#fff";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "600 " + px + "px system-ui, sans-serif";
    ctx.fillText(letter, size / 2, size / 2 + Math.round(px * 0.04));
  }

  /*
   * IconBank remembers a label and an optional image for each app id and can
   * render the pixel blob the band asks for, at whatever size it requests.
   *   set(appId, { label, image })  image is an HTMLImageElement (optional)
   *   pixelsFor(appId, url, size)   -> Uint8Array band raster
   */
  class IconBank {
    constructor() {
      this.entries = new Map();
      this.fallback = null; // { label }
    }

    set(appId, label, image) {
      this.entries.set(appId, { label, image });
    }
    remove(appId) {
      this.entries.delete(appId);
    }
    // Clear, so a re-sent notification forces a fresh icon.
    markDirty(appId) { /* entries stay but served again is fine */ }

    _source(appId) {
      return this.entries.get(appId) || { label: appId };
    }

    // Draw the stored image (or letter tile) into `size` px canvas.
    _render(appId, size) {
      const canvas = makeCanvas(size);
      const ctx = canvas.getContext("2d");
      const src = this._source(appId);
      ctx.clearRect(0, 0, size, size);
      if (src.image && src.image.complete && src.image.naturalWidth > 0) {
        // Cover-fit the square, then read.
        const iw = src.image.naturalWidth, ih = src.image.naturalHeight;
        const scale = Math.max(size / iw, size / ih);
        const w = iw * scale, h = ih * scale;
        ctx.drawImage(src.image, (size - w) / 2, (size - h) / 2, w, h);
      } else {
        drawLetterTile(canvas, src.label || appId, appId);
      }
      return canvas;
    }

    pixelsFor(appId, url, size) {
      const canvas = this._render(appId, size);
      const ctx = canvas.getContext("2d");
      const imageData = ctx.getImageData(0, 0, size, size);
      return toBandPixels(imageData, size);
    }
  }

  window.GF3Icons = { toBandPixels, drawLetterTile, IconBank, colorFor };
})();
