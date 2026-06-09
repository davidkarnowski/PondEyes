// webapp/render.js
// ================
// Canvas drawing for the live + playback views, mirroring radar/render.py's visual language:
// per-sensor FOV cone, min/max distance-gate arcs + a faint filled wedge, 0.5 m range rings with
// metre labels, pulse-ring targets (translucent when "ghosted"), and fading trails. Everything is
// drawn from WORLD-mm via the View transform, so it pans/zooms with the map.

import { fovArcPts, sensorFillPts } from "./geometry.js";

const RING_STEP_MM = 500;

export function dim(rgb, f) { return [Math.round(rgb[0] * f), Math.round(rgb[1] * f), Math.round(rgb[2] * f)]; }
export function hexToRgb(h) {
  h = (h || "#00ff88").replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
const css = (rgb, a = 1) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;

function polyline(ctx, pts, color, width = 1, close = false) {
  if (!pts || pts.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  if (close) ctx.closePath();
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
}

// One sensor: marker, FOV cone edges, gate arcs, faint fill, range rings + labels, label.
export function drawSensor(ctx, view, s, { drawFill = true, drawRings = true } = {}) {
  const rgb = hexToRgb(s.color);
  const [sx, sy] = view.worldToPx(s.x, s.y);
  const fov = s.fov_deg || 120;
  const ppm = view.ppm;
  const maxR = (s.max_range_mm > 0 ? s.max_range_mm : 5000) * ppm;   // cone reaches max gate (or 5 m)
  const minR = (s.min_range_mm > 0 ? s.min_range_mm : 0) * ppm;

  // faint filled active wedge
  if (drawFill && s.max_range_mm > 0) {
    const fill = sensorFillPts(sx, sy, s.heading, maxR, minR, fov);
    if (fill) {
      ctx.beginPath();
      ctx.moveTo(fill[0][0], fill[0][1]);
      for (let i = 1; i < fill.length; i++) ctx.lineTo(fill[i][0], fill[i][1]);
      ctx.closePath();
      ctx.fillStyle = css(rgb, 0.12); ctx.fill();
    }
  }
  // cone edges (two straight lines from the sensor out to the FOV edges at maxR)
  const edge = fovArcPts(sx, sy, s.heading, maxR, fov, 2);   // 3 pts: left, mid, right
  polyline(ctx, [edge[0], [sx, sy], edge[edge.length - 1]], css(rgb, 0.9), 1);

  // range rings every 0.5 m within the active band, metre labels on whole metres
  if (drawRings && s.max_range_mm > 0) {
    const ring = css(dim(rgb, 0.4));
    ctx.font = "11px monospace"; ctx.fillStyle = ring;
    for (let r = RING_STEP_MM; r <= s.max_range_mm + 1; r += RING_STEP_MM) {
      if (r <= s.min_range_mm) continue;
      polyline(ctx, fovArcPts(sx, sy, s.heading, r * ppm, fov), ring, 1);
      if (r % 1000 === 0) {
        const th = s.heading * Math.PI / 180;
        const lx = sx + Math.sin(th) * r * ppm, ly = sy - Math.cos(th) * r * ppm;
        ctx.fillText(`${r / 1000}m`, lx + 3, ly - 5);
      }
    }
  }
  // min/max gate arcs (brighter than rings)
  if (s.max_range_mm > 0) polyline(ctx, fovArcPts(sx, sy, s.heading, s.max_range_mm * ppm, fov), css(rgb, 0.8), 2);
  if (s.min_range_mm > 0) polyline(ctx, fovArcPts(sx, sy, s.heading, s.min_range_mm * ppm, fov), css(rgb, 0.8), 2);

  // marker + label
  ctx.beginPath(); ctx.arc(sx, sy, 6, 0, 2 * Math.PI); ctx.fillStyle = css(rgb); ctx.fill();
  if (s.label) { ctx.font = "13px monospace"; ctx.fillStyle = css(rgb); ctx.fillText(s.label, sx + 9, sy - 9); }
  return [sx, sy];
}

// One target: expanding pulse ring (scaled by speed) + dot + serial; translucent + grey when ghost.
export function drawTarget(ctx, px, py, rgb, { pulse = 0, speedNorm = 0, label = "", ghost = false } = {}) {
  const ringR = 10 + pulse * (20 + 60 * speedNorm);
  const c = ghost ? dim(rgb, 0.6) : rgb;
  const a = ghost ? 0.35 : 1;
  ctx.beginPath(); ctx.arc(px, py, ringR, 0, 2 * Math.PI);
  ctx.strokeStyle = css(c, a); ctx.lineWidth = 1; ctx.stroke();
  ctx.beginPath(); ctx.arc(px, py, 5, 0, 2 * Math.PI); ctx.fillStyle = css(c, a); ctx.fill();
  if (label) { ctx.font = "13px monospace"; ctx.fillStyle = css(c, a); ctx.fillText(label, px + 8, py - 8); }
}

// Fading trail: a deque of [px,py,t] points drawn with alpha falling off by age.
export function drawTrail(ctx, pts, now, duration, rgb) {
  for (const [px, py, t] of pts) {
    const age = now - t;
    if (age > duration) continue;
    const a = Math.max(0, 1 - age / duration) * 0.6;
    ctx.beginPath(); ctx.arc(px, py, 2.5, 0, 2 * Math.PI); ctx.fillStyle = css(rgb, a); ctx.fill();
  }
}
