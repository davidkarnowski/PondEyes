// webapp/app.js
// =============
// The PondEyes browser frontend. Connects to /ws/live (WORLD-mm snapshots), renders the SVG map +
// sensors/targets/trails on a canvas with native pan/zoom/pinch, mirrors the pygame top-menu
// toggles, and drives the CONFIG + PLAYBACK drawers off the REST API.

import { View } from "./geometry.js";
import { drawSensor, drawTarget, drawTrail, hexToRgb } from "./render.js";

const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");
const view = new View();

const state = {
  snap: { sensors: [], targets: [], recent: [], stats: {}, data_lost: false },
  mapImg: null,
  trails: new Map(),     // serial -> [[mx,my,t], ...] world-mm history
  pulse: new Map(),      // serial -> animation phase
  ghost: new Set(),      // serials drawn translucent (false reading)
  silence: new Set(),    // serials muted (for the future Web-Audio beep)
  globals: {},           // sound / night / trail_on / smoothing_on ...
  trailDuration: 5.0,
  night: false,
  playback: null,        // {points, t0, t, playing, color, sensor} when replaying
  lastFrame: 0,
};

// ── sizing ────────────────────────────────────────────────────────────────────────────────
function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = innerWidth * dpr; canvas.height = innerHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (state.mapImg) view.fit(innerWidth, innerHeight, view.mapW, view.mapH);
}
addEventListener("resize", resize);

// ── data: map + live feed ───────────────────────────────────────────────────────────────────
async function loadMap() {
  const meta = await (await fetch("/api/map/meta")).json();
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = "/api/map.svg"; });
  state.mapImg = img;
  view.fit(innerWidth, innerHeight, meta.width_mm, meta.height_mm);
}

function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws/live`);
  ws.onmessage = (e) => { state.snap = JSON.parse(e.data); };
  ws.onclose = () => setTimeout(connectWS, 1000);   // auto-reconnect
}

// ── render loop ──────────────────────────────────────────────────────────────────────────────
function frame(tms) {
  const now = tms / 1000;
  const dt = state.lastFrame ? now - state.lastFrame : 0;
  state.lastFrame = now;

  ctx.clearRect(0, 0, innerWidth, innerHeight);
  // map background, scaled by the view (full-bleed; the canvas itself is the clip)
  if (state.mapImg) {
    const w = view.mapW * view.ppm, h = view.mapH * view.ppm;
    ctx.drawImage(state.mapImg, view.offX, view.offY, w, h);
  }

  const snap = state.playback ? playbackSnap(now) : state.snap;

  for (const s of snap.sensors) drawSensor(ctx, view, s);

  // accumulate + draw trails (world-mm history -> px so they pan/zoom correctly)
  const liveSerials = new Set();
  for (const t of snap.targets) {
    liveSerials.add(t.serial);
    const hist = state.trails.get(t.serial) || [];
    hist.push([t.x, t.y, now]);
    while (hist.length && now - hist[0][2] > state.trailDuration) hist.shift();
    state.trails.set(t.serial, hist);
  }
  const sensorColor = Object.fromEntries(snap.sensors.map((s) => [s.id, hexToRgb(s.color)]));
  if (state.globals.trail_on !== false) {
    for (const [ser, hist] of state.trails) {
      if (!liveSerials.has(ser)) continue;
      const rgb = sensorColor[serialSensor(snap, ser)] || [0, 255, 102];
      const pts = hist.map(([mx, my, t]) => { const [px, py] = view.worldToPx(mx, my); return [px, py, t]; });
      drawTrail(ctx, pts, now, state.trailDuration, rgb);
    }
  }
  // targets
  for (const t of snap.targets) {
    const rgb = sensorColor[t.sensor_id] || [0, 255, 102];
    const ph = ((state.pulse.get(t.serial) || 0) + dt) % 1;
    state.pulse.set(t.serial, ph);
    const [px, py] = view.worldToPx(t.x, t.y);
    drawTarget(ctx, px, py, rgb, {
      pulse: ph, speedNorm: Math.min(1, (t.v || 0) / 4000),
      label: t.serial, ghost: state.ghost.has(t.serial),
    });
  }
  if (state.night) { ctx.fillStyle = "rgba(255,0,0,0.30)"; ctx.fillRect(0, 0, innerWidth, innerHeight); }

  updateHud(snap);
  requestAnimationFrame(frame);
}

function serialSensor(snap, ser) {
  const t = snap.targets.find((x) => x.serial === ser);
  return t ? t.sensor_id : null;
}

// ── HUD ────────────────────────────────────────────────────────────────────────────────────
function updateHud(snap) {
  const recent = document.getElementById("recent");
  recent.innerHTML = "Recent Targets:<br>" + (snap.recent || []).slice(0, 6).map((r) =>
    `${r.sensor_id || ""}/${r.serial}: ${(r.first || "").slice(11, 19)}–${(r.last || "").slice(11, 19)} (${r.dur})`
  ).join("<br>");
  const dbg = (snap.sensors || []).map((s) => {
    const st = (snap.stats || {})[s.id] || {}; return `${s.id}:${(st.hz || 0).toFixed(1)}Hz`;
  }).join("  ");
  document.getElementById("debug").textContent =
    `${dbg}  targets=${(snap.targets || []).length}`;
  document.getElementById("clock").textContent =
    state.playback ? state.pbClock || "" : new Date().toLocaleTimeString("en-GB");
  document.getElementById("banner").hidden = !snap.data_lost || !!state.playback;
}

// ── gestures: pan (drag / two-finger scroll), zoom (wheel+ctrl / pinch / slider) ───────────────
let dragging = false, lastX = 0, lastY = 0;
canvas.addEventListener("pointerdown", (e) => { dragging = true; lastX = e.clientX; lastY = e.clientY; canvas.setPointerCapture(e.pointerId); });
canvas.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  view.panBy(e.clientX - lastX, e.clientY - lastY); lastX = e.clientX; lastY = e.clientY;
});
canvas.addEventListener("pointerup", () => { dragging = false; });
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {                          // pinch-zoom (browsers send pinch as wheel+ctrl)
    view.zoomAbout(Math.pow(1.0025, -e.deltaY), e.clientX, e.clientY);
  } else {                                                // two-finger scroll -> pan
    view.panBy(-e.deltaX, -e.deltaY);
  }
  syncZoomLabel();
}, { passive: false });

const zoom = document.getElementById("zoom");
zoom.addEventListener("input", () => { view.zoom = +zoom.value; view.panX = 0; view.panY = 0; syncZoomLabel(); });
document.getElementById("resetview").onclick = () => { view.reset(); syncZoomLabel(); };
function syncZoomLabel() { document.getElementById("zoomlabel").textContent = view.zoom.toFixed(1) + "x"; zoom.value = view.zoom; }

// ── target right-click context menu (Silence / Ghost) ──────────────────────────────────────────
canvas.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const ser = targetAt(e.clientX, e.clientY);
  const m = document.getElementById("ctxmenu");
  if (!ser) { m.hidden = true; return; }
  m.dataset.serial = ser; m.style.left = e.clientX + "px"; m.style.top = e.clientY + "px"; m.hidden = false;
  m.querySelector('[data-ctx="silence"]').textContent = state.silence.has(ser) ? "Unsilence" : "Silence";
  m.querySelector('[data-ctx="ghost"]').textContent = state.ghost.has(ser) ? "Unghost" : "Ghost";
});
document.getElementById("ctxmenu").addEventListener("click", (e) => {
  const m = document.getElementById("ctxmenu"); const ser = m.dataset.serial; const act = e.target.dataset.ctx;
  if (act === "silence") toggleSet(state.silence, ser);
  if (act === "ghost") toggleSet(state.ghost, ser);
  m.hidden = true;
});
addEventListener("click", (e) => { if (!e.target.closest("#ctxmenu")) document.getElementById("ctxmenu").hidden = true; });
function toggleSet(set, v) { set.has(v) ? set.delete(v) : set.add(v); }
function targetAt(cx, cy) {
  let best = null, bd = 18;
  for (const t of state.snap.targets) {
    const [px, py] = view.worldToPx(t.x, t.y);
    const d = Math.hypot(cx - px, cy - py);
    if (d < bd) { bd = d; best = t.serial; }
  }
  return best;
}

// ── menu toggles + drawers ─────────────────────────────────────────────────────────────────
async function postConfig(patch) {
  await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
}
document.querySelectorAll("#menu [data-toggle]").forEach((btn) => {
  btn.onclick = async () => {
    const key = btn.dataset.toggle; const val = !state.globals[key]; state.globals[key] = val;
    if (key === "night") state.night = val;
    if (key === "trail_on" && !val) state.trails.clear();
    refreshToggles(); await postConfig({ [key]: val });
  };
});
document.querySelector('[data-act="config"]').onclick = () => openConfig();
document.querySelector('[data-act="playback"]').onclick = () => openPlayback();
document.querySelector('[data-act="fullscreen"]').onclick = () =>
  document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
function refreshToggles() {
  document.querySelectorAll("#menu [data-toggle]").forEach((b) => {
    const k = b.dataset.toggle, on = !!state.globals[k];
    b.classList.toggle("off", !on && k !== "night");
    b.classList.toggle("on", on && k !== "night");
    b.classList.toggle("night-on", k === "night" && on);
  });
}

// ── CONFIG drawer ──────────────────────────────────────────────────────────────────────────
async function openConfig() {
  const cfg = await (await fetch("/api/config")).json();
  state.globals = cfg.globals || {}; state.night = !!state.globals.night;
  state.trailDuration = +(state.globals.trail_duration ?? 5.0); refreshToggles();
  const list = document.getElementById("sensor-list"); list.innerHTML = "";
  for (const s of cfg.sensors) list.appendChild(sensorCard(s));
  document.getElementById("config").hidden = false;
}
function field(label, input) {
  const row = document.createElement("div"); row.className = "row";
  const l = document.createElement("label"); l.textContent = label; row.append(l, input); return row;
}
function sensorCard(s) {
  const card = document.createElement("div"); card.className = "sensor-card"; card.dataset.id = s.id;
  const mk = (type, val) => { const i = document.createElement("input"); i.type = type; i.value = val; return i; };
  const mode = document.createElement("select");
  for (const m of ["serial", "mqtt", "sim"]) { const o = document.createElement("option"); o.value = o.textContent = m; if (m === s.input_mode) o.selected = true; mode.append(o); }
  const head = document.createElement("strong"); head.textContent = `${s.id} — ${s.label}`;
  card.append(head,
    field("Input", mode),
    field("Serial port", mk("text", s.serial_port || "")),
    field("Baud", mk("number", s.serial_baud || 256000)),
    field("Color", mk("color", s.color)),
    field("Min range m", mk("number", (s.min_range_mm || 0) / 1000)),
    field("Max range m", mk("number", (s.max_range_mm || 0) / 1000)));
  card._read = () => ({
    id: s.id, label: s.label, input_mode: mode.value,
    serial_port: card.querySelectorAll("input")[0].value,
    serial_baud: +card.querySelectorAll("input")[1].value,
    color: card.querySelectorAll("input")[2].value,
    min_range_mm: (+card.querySelectorAll("input")[3].value) * 1000,
    max_range_mm: (+card.querySelectorAll("input")[4].value) * 1000,
    position: s.position, heading: s.heading,
    sim_pattern: s.sim_pattern, sim_hz: s.sim_hz, broker: s.broker, port: s.port, topic: s.topic,
    speed_sensitivity: s.speed_sensitivity, trail_on: s.trail_on, enabled: s.enabled,
  });
  return card;
}
document.getElementById("config-x").onclick = () => (document.getElementById("config").hidden = true);
document.getElementById("config-apply").onclick = async () => {
  const sensors = [...document.querySelectorAll(".sensor-card")].map((c) => c._read());
  const r = await (await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sensors }) })).json();
  document.getElementById("config-status").textContent = r.cfg_error || "applied ✓";
};

// ── PLAYBACK drawer ────────────────────────────────────────────────────────────────────────
async function openPlayback() {
  const { tracks } = await (await fetch("/api/tracks")).json();
  const list = document.getElementById("track-list"); list.innerHTML = "";
  for (const t of tracks.slice(0, 40)) {
    const div = document.createElement("div"); div.className = "track";
    div.textContent = `${t.sensor_id}/${t.serial}  ${(t.first_seen || "").slice(0, 19)}  pts=${t.point_count ?? "?"}`;
    div.onclick = () => loadTrack(t.id);
    list.appendChild(div);
  }
  document.getElementById("playback").hidden = false;
}
async function loadTrack(id) {
  const d = await (await fetch(`/api/tracks/${id}`)).json();
  const pts = d.points.map((p) => ({ t: p.t_rel_s ?? 0, x: p.x_mm, y: p.y_mm, v: p.speed_mm_s }));
  state.playback = { track: d.track, points: pts, start: performance.now() / 1000, playing: true, idx: 0 };
  document.getElementById("playback-controls").hidden = false;
}
function playbackSnap(now) {
  const pb = state.playback; const tr = pb.track;
  const dur = pb.points.length ? pb.points[pb.points.length - 1].t : 0;
  if (pb.playing) pb.t = (now - pb.start) % (dur || 1);
  // nearest point at/under pb.t
  let cur = pb.points[0];
  for (const p of pb.points) { if (p.t <= (pb.t || 0)) cur = p; else break; }
  document.getElementById("pb-scrub").value = dur ? (pb.t / dur) * 100 : 0;
  const wall = tr.first_seen ? new Date(new Date(tr.first_seen).getTime() + (pb.t || 0) * 1000) : null;
  state.pbClock = (wall ? wall.toLocaleTimeString("en-GB") + "  " : "") + `(+${(pb.t || 0).toFixed(1)}s)`;
  const sensor = { id: tr.sensor_id, label: tr.serial, color: tr.sensor_color || "#00ff88",
    x: tr.sensor_x, y: tr.sensor_y, heading: tr.sensor_heading,
    min_range_mm: tr.sensor_min_range_mm || 0, max_range_mm: tr.sensor_max_range_mm || 0, fov_deg: 120 };
  // recorded points are sensor-LOCAL; project through the recorded pose (mirror local_to_world)
  const [wx, wy] = localToWorld(cur.x, cur.y, tr.sensor_x, tr.sensor_y, tr.sensor_heading);
  return { sensors: [sensor], targets: [{ sensor_id: tr.sensor_id, serial: tr.serial, x: wx, y: wy, v: cur.v }],
    recent: [], stats: {}, data_lost: false };
}
function localToWorld(xl, yl, sx, sy, hd) {
  const th = hd * Math.PI / 180, c = Math.cos(th), s = Math.sin(th);
  return [sx + xl * c + yl * s, sy - xl * s + yl * c];
}
document.getElementById("playback-x").onclick = () => { state.playback = null; document.getElementById("playback").hidden = true; document.getElementById("playback-controls").hidden = true; };
document.getElementById("pb-play").onclick = (e) => { if (state.playback) { state.playback.playing = !state.playback.playing; state.playback.start = performance.now() / 1000 - (state.playback.t || 0); e.target.textContent = state.playback.playing ? "⏸" : "▶"; } };
document.getElementById("pb-scrub").oninput = (e) => { if (state.playback) { const dur = state.playback.points.at(-1)?.t || 0; state.playback.t = (e.target.value / 100) * dur; state.playback.playing = false; state.playback.start = performance.now() / 1000 - state.playback.t; } };

// ── boot ──────────────────────────────────────────────────────────────────────────────────
(async function () {
  resize();
  try { await loadMap(); } catch (e) { console.error("map load failed", e); }
  try { state.globals = (await (await fetch("/api/config")).json()).globals || {}; state.night = !!state.globals.night; state.trailDuration = +(state.globals.trail_duration ?? 5.0); refreshToggles(); } catch (e) {}
  connectWS();
  requestAnimationFrame(frame);
})();
