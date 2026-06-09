// webapp/app.js
// =============
// The PondEyes browser frontend. Connects to /ws/live (WORLD-mm snapshots), renders the SVG map +
// sensors/targets/trails on a canvas with native pan/zoom/pinch, mirrors the pygame top-menu
// toggles, and drives the CONFIG + PLAYBACK drawers off the REST API.

import { View, fovArcPts } from "./geometry.js";
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
  sel: null,             // selected sensor id (shows the heading handle; enables drag-edit)
  editDrag: null,        // {id, type:'move'|'aim'|'min'|'max'} during a sensor edit drag
  override: null,        // {id, ...fields} live-preview override applied over the WS snapshot
  ctxSerial: null,       // serial the right-click menu acts on (Silence/Ghost)
  ctxWorld: null,        // [mx,my] world point the menu is LOCKED to — floats with the map, not the target
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

  // live-preview the sensor being edited (override applied over the server snapshot)
  const sensors = state.playback ? snap.sensors : snap.sensors.map(withOverride);
  for (const s of sensors) drawSensor(ctx, view, s, { label: false });
  drawLabels(sensors);                                    // labels last, auto-offset when close
  if (!state.playback && state.sel) {                     // grabbable heading handle + selection ring
    const s = sensors.find((x) => x.id === state.sel);
    if (s) drawEditHandles(ctx, s);
  }

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
  const sensorTrail = Object.fromEntries(snap.sensors.map((s) => [s.id, s.trail_on !== false]));
  const ghostedNow = new Set(snap.targets.filter((t) => t.ghost).map((t) => t.serial));
  if (state.globals.trail_on !== false) {            // master trail toggle AND per-sensor trail_on
    for (const [ser, hist] of state.trails) {
      if (!liveSerials.has(ser)) continue;
      if (ghostedNow.has(ser)) continue;             // ghosted (false reading) -> no trail
      const sid = serialSensor(snap, ser);
      if (sensorTrail[sid] === false) continue;
      const rgb = sensorColor[sid] || [0, 255, 102];
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
      label: t.serial, ghost: !!t.ghost,             // ghost/silence are backend-shared snapshot flags
    });
  }
  if (state.night) { ctx.fillStyle = "rgba(255,0,0,0.30)"; ctx.fillRect(0, 0, innerWidth, innerHeight); }

  if (!state.playback) beep(snap, now);
  positionCtxMenu();                                      // float the context menu with its target
  updateHud(snap);
  requestAnimationFrame(frame);
}

function serialSensor(snap, ser) {
  const t = snap.targets.find((x) => x.serial === ser);
  return t ? t.sensor_id : null;
}

// Sensor name labels drawn last with greedy push-down so close/overlapping sensors stay readable.
function drawLabels(sensors) {
  ctx.font = "13px monospace";
  const placed = [];
  for (const s of sensors) {
    if (!s.label) continue;
    const [mx, my] = view.worldToPx(s.x, s.y);
    let lx = mx + 9, ly = my - 9; const w = ctx.measureText(s.label).width;
    let guard = 0;
    while (guard++ < 12 && placed.some((p) => Math.abs(p.x - lx) < Math.max(p.w, w) && Math.abs(p.y - ly) < 15)) ly += 16;
    placed.push({ x: lx, y: ly, w });
    ctx.fillStyle = s.color || "#00ff66"; ctx.fillText(s.label, lx, ly);
  }
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
  const aud = audioCtx ? audioCtx.state : "none";
  document.getElementById("debug").textContent =
    `${dbg}  targets=${(snap.targets || []).length}  audio=${aud}/${state.globals.sound ? "SOUND-on" : "SOUND-off"}`;
  document.getElementById("clock").textContent = new Date().toLocaleTimeString("en-GB");
  document.getElementById("banner").hidden = !snap.data_lost || !!state.playback;
}

// ── sensor edit on the map (click marker to select; drag marker=move, handle=aim, arc=gate) ────
const HANDLE_PX = 52;
function liveSensors() { return state.playback ? [] : state.snap.sensors; }
function markerPx(s) { return view.worldToPx(s.x, s.y); }
function handlePx(s) {
  const [mx, my] = markerPx(s); const th = s.heading * Math.PI / 180;
  return [mx + Math.sin(th) * HANDLE_PX, my - Math.cos(th) * HANDLE_PX];
}
function sensorById(id) { return liveSensors().find((s) => s.id === id); }
function sensorMarkerAt(cx, cy) {
  for (const s of liveSensors()) { const [mx, my] = markerPx(s); if (Math.hypot(cx - mx, cy - my) <= 10) return s.id; }
  return null;
}
function gateArcAt(cx, cy) {
  for (const s of liveSensors()) {
    const [mx, my] = markerPx(s); const d = Math.hypot(cx - mx, cy - my);
    const ang = Math.atan2(cx - mx, -(cy - my)) * 180 / Math.PI;            // 0 = up (heading frame)
    const da = Math.abs(((ang - s.heading + 540) % 360) - 180);            // 0..180 from heading
    if (da > (s.fov_deg || 120) / 2 + 6) continue;
    for (const which of ["max", "min"]) {
      const r = which === "max" ? s.max_range_mm : s.min_range_mm;
      if (r > 0 && Math.abs(d - r * view.ppm) <= 8) return { id: s.id, type: which };
    }
  }
  return null;
}
function ptSegDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  if (dx === 0 && dy === 0) return Math.hypot(px - ax, py - ay);
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
function coneLineAt(cx, cy, tol = 6) {     // a sensor's FOV fence (cone edge) line — for stacked markers
  for (const s of liveSensors()) {
    const [mx, my] = markerPx(s);
    const R = (s.max_range_mm > 0 ? s.max_range_mm : 5000) * view.ppm;
    const edge = fovArcPts(mx, my, s.heading, R, s.fov_deg || 120, 2);      // [left, mid, right]
    for (const end of [edge[0], edge[edge.length - 1]]) {
      if (ptSegDist(cx, cy, mx, my, end[0], end[1]) <= tol) return s.id;
    }
  }
  return null;
}

let panning = false, lastX = 0, lastY = 0;
canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId);
  if (state.sel) {                                  // 1) heading handle of the selected sensor -> aim
    const s = sensorById(state.sel);
    if (s) { const [hx, hy] = handlePx(s); if (Math.hypot(e.clientX - hx, e.clientY - hy) <= 12) { state.editDrag = { id: s.id, type: "aim" }; return; } }
  }
  const arc = gateArcAt(e.clientX, e.clientY);       // 2) gate arc -> resize (also selects)
  if (arc) { state.sel = arc.id; state.editDrag = arc; return; }
  const sid = sensorMarkerAt(e.clientX, e.clientY);  // 3) marker -> select + allow move-drag
  if (sid) { state.sel = sid; state.editDrag = { id: sid, type: "move", moved: false }; return; }
  state.sel = null; panning = true; lastX = e.clientX; lastY = e.clientY;   // 4) empty -> deselect + pan
});
canvas.addEventListener("pointermove", (e) => {
  if (state.editDrag) { applyEditDrag(e.clientX, e.clientY); return; }
  if (panning) { view.panBy(e.clientX - lastX, e.clientY - lastY); lastX = e.clientX; lastY = e.clientY; return; }
  updateCursor(e.clientX, e.clientY);                 // hover cursor over clickable tools
});
canvas.addEventListener("pointerup", () => { if (state.editDrag) commitEditDrag(); panning = false; });
// double-click a FOV fence line (or marker) to select a sensor — reaches stacked/overlapping markers
canvas.addEventListener("dblclick", (e) => {
  const sid = coneLineAt(e.clientX, e.clientY) || sensorMarkerAt(e.clientX, e.clientY);
  if (sid) state.sel = sid;
});
function updateCursor(cx, cy) {
  let cur = "grab";                                   // empty map = pannable
  if (state.sel) { const s = sensorById(state.sel); if (s) { const [hx, hy] = handlePx(s); if (Math.hypot(cx - hx, cy - hy) <= 12) cur = "move"; } }
  if (cur === "grab") {
    if (gateArcAt(cx, cy)) cur = "ew-resize";
    else if (sensorMarkerAt(cx, cy)) cur = "move";
    else if (coneLineAt(cx, cy)) cur = "pointer";
  }
  canvas.style.cursor = cur;
}

function applyEditDrag(cx, cy) {
  const d = state.editDrag, s = sensorById(d.id); if (!s) return;
  if (d.type === "move") { const [wx, wy] = view.pxToWorld(cx, cy); state.override = { id: d.id, x: wx, y: wy }; d.moved = true; }
  else if (d.type === "aim") { const [mx, my] = markerPx(s); const hd = Math.atan2(cx - mx, -(cy - my)) * 180 / Math.PI; state.override = { id: d.id, heading: ((hd + 540) % 360) - 180 }; }
  else { const [mx, my] = markerPx(s); const mm = Math.max(0, Math.min(8000, Math.hypot(cx - mx, cy - my) / view.ppm));
         state.override = { id: d.id, [d.type === "max" ? "max_range_mm" : "min_range_mm"]: mm }; }   // cap at 8 m
}
async function commitEditDrag() {
  const d = state.editDrag; state.editDrag = null;
  if (d.type === "move" && !d.moved) { state.override = null; return; }     // plain click = select only
  const ov = state.override; if (!ov || ov.id !== d.id) { state.override = null; return; }
  const patch = {};
  if (d.type === "move") patch.position = [Math.round(ov.x), Math.round(ov.y)];
  else if (d.type === "aim") patch.heading = Math.round(ov.heading * 10) / 10;
  else { const k = d.type === "max" ? "max_range_mm" : "min_range_mm"; patch[k] = Math.round(ov[k]); }
  await fetch(`/api/sensors/${d.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  setTimeout(() => { if (!state.editDrag) state.override = null; }, 250);    // hold until the WS catches up
}
function withOverride(s) { return state.override && state.override.id === s.id ? { ...s, ...state.override } : s; }
function drawEditHandles(c, s) {
  const col = s.color || "#00ff66";                                        // handle = the marker's colour
  const [mx, my] = view.worldToPx(s.x, s.y), [hx, hy] = handlePx(s);
  c.strokeStyle = col; c.fillStyle = col; c.lineWidth = 2;
  c.beginPath(); c.arc(mx, my, 11, 0, 2 * Math.PI); c.stroke();             // selection ring
  c.beginPath(); c.moveTo(mx, my); c.lineTo(hx, hy); c.stroke();           // heading arm
  c.beginPath(); c.arc(hx, hy, 6, 0, 2 * Math.PI); c.fill();               // grabbable aim handle
}

// ── proximity beep (Web Audio) — fastest in-range, non-silenced/ghosted target; SOUND-gated ────
let audioCtx = null, lastBeep = 0, lastTargetSeen = 0, lastN = 0;
function ensureAudio() {
  // Browsers block audio until a user gesture; create + resume the context on any interaction.
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state !== "running") audioCtx.resume();
  } catch (e) { /* no audio available */ }
}
addEventListener("pointerdown", ensureAudio);   // unlock audio on the first click/touch anywhere
addEventListener("keydown", ensureAudio);
function beep(snap, now) {
  if (!state.globals.sound || !audioCtx) return;
  if (audioCtx.state !== "running") { audioCtx.resume(); return; }      // still locked — wake + skip
  // SAME method as the pygame UI: speed-based. n = fastest audible target's speed / MAX_V (4000);
  // freq 700..1400 Hz; interval 0.6 - n*0.45 s. The only web-specific addition is a brief HOLD
  // below, because the websocket path occasionally blinks a track out for a frame (the in-process
  // pygame UI doesn't), which made the beep stutter.
  const audible = snap.targets.filter((t) => !t.silence && !t.ghost);
  const any = audible.length > 0;
  let n = Math.min(1, audible.reduce((m, t) => Math.max(m, t.v || 0), 0) / 4000);
  if (any) { lastTargetSeen = now; lastN = n; }
  if (now - lastTargetSeen > 0.6) return;                               // truly no target -> silent
  n = any ? n : lastN;                                                  // ride out a brief dropout
  if (now - lastBeep < 0.6 - n * 0.45) return;
  lastBeep = now;
  try {
    const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
    osc.frequency.value = 700 + 700 * n; g.gain.value = 0.15;
    osc.connect(g); g.connect(audioCtx.destination);
    osc.start(); osc.stop(audioCtx.currentTime + 0.06);
  } catch (e) { /* ignore */ }
}
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {                          // pinch-zoom (browsers send pinch as wheel+ctrl)
    view.zoomAbout(Math.pow(1.012, -e.deltaY), e.clientX, e.clientY);   // ~5x more zoom per gesture
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
function targetFlags(ser) { return state.snap.targets.find((t) => t.serial === ser) || { ghost: false, silence: false }; }
canvas.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const ser = targetAt(e.clientX, e.clientY);
  const m = document.getElementById("ctxmenu");
  if (!ser) { m.hidden = true; return; }
  const f = targetFlags(ser);
  m.dataset.serial = ser; state.ctxSerial = ser; m.hidden = false;
  state.ctxWorld = view.pxToWorld(e.clientX, e.clientY);   // lock to the MAP point under the click
  m.querySelector('[data-ctx="silence"]').textContent = f.silence ? "Unsilence" : "Silence";
  m.querySelector('[data-ctx="ghost"]').textContent = f.ghost ? "Unghost" : "Ghost";
  positionCtxMenu();                                  // place it (anchored to the map point, clamped)
});
// Keep the menu locked to the MAP point it opened on (so it moves with pan/zoom, NOT the target),
// and clamped on-screen.
function positionCtxMenu() {
  const m = document.getElementById("ctxmenu");
  if (m.hidden || !state.ctxWorld) return;
  const [px, py] = view.worldToPx(state.ctxWorld[0], state.ctxWorld[1]);
  const r = m.getBoundingClientRect();
  const x = Math.max(4, Math.min(innerWidth - r.width - 4, px + 12));
  const y = Math.max(4, Math.min(innerHeight - r.height - 4, py + 12));
  m.style.left = x + "px"; m.style.top = y + "px";
}
document.getElementById("ctxmenu").addEventListener("click", async (e) => {
  const m = document.getElementById("ctxmenu"); const ser = m.dataset.serial; const act = e.target.dataset.ctx;
  const f = targetFlags(ser);
  const patch = act === "silence" ? { silence: !f.silence } : act === "ghost" ? { ghost: !f.ghost } : null;
  m.hidden = true; state.ctxSerial = null; state.ctxWorld = null;   // ghost/silence are BACKEND state -> POST
  if (patch) await fetch(`/api/targets/${ser}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
});
addEventListener("click", (e) => { if (!e.target.closest("#ctxmenu")) { document.getElementById("ctxmenu").hidden = true; state.ctxSerial = null; state.ctxWorld = null; } });
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
function field(label, input) {
  const row = document.createElement("div"); row.className = "row";
  const l = document.createElement("label"); l.textContent = label; row.append(l, input); return row;
}
function mkInput(type, val, attrs = {}) {
  const i = document.createElement("input"); i.type = type; i.value = val; Object.assign(i, attrs); return i;
}
function nextSensorId() {
  const ids = [...document.querySelectorAll(".sensor-card")].map((c) => c.dataset.id);
  let n = 1; while (ids.includes("S" + n)) n++; return "S" + n;
}
function sensorCard(s) {
  const card = document.createElement("div"); card.className = "sensor-card"; card.dataset.id = s.id;
  const f = {};
  f.label = mkInput("text", s.label || s.id);
  f.mode = document.createElement("select");
  for (const m of ["serial", "mqtt", "sim"]) { const o = document.createElement("option"); o.value = o.textContent = m; if (m === s.input_mode) o.selected = true; f.mode.append(o); }
  f.port = mkInput("text", s.serial_port || "");
  f.baud = mkInput("number", s.serial_baud || 256000);
  f.color = mkInput("color", s.color || "#00ddff");
  f.posx = mkInput("number", s.position ? s.position[0] : 0);
  f.posy = mkInput("number", s.position ? s.position[1] : 0);
  f.head = mkInput("number", s.heading ?? 0);
  f.min = mkInput("number", (s.min_range_mm || 0) / 1000, { step: 0.1, min: 0, max: 8 });
  f.max = mkInput("number", (s.max_range_mm || 0) / 1000, { step: 0.1, min: 0, max: 8 });   // 8 m design max
  f.sens = mkInput("range", s.speed_sensitivity ?? 0.25, { min: 0, max: 1, step: 0.01 });
  const hd = document.createElement("div"); hd.className = "card-head";
  const title = document.createElement("strong"); title.textContent = s.id;
  const dup = document.createElement("button"); dup.textContent = "⊕ DUP";
  dup.onclick = () => { const v = card._read(); const id = nextSensorId(); card.after(sensorCard({ ...v, id, label: id })); };
  const rm = document.createElement("button"); rm.textContent = "− REMOVE"; rm.onclick = () => card.remove();
  hd.append(title, dup, rm);
  // AUTO-baud button beside the Baud field — probes the (possibly edited) port server-side.
  const auto = document.createElement("button"); auto.type = "button"; auto.textContent = "AUTO";
  auto.onclick = async () => {
    auto.textContent = "…";
    const r = await (await fetch(`/api/sensors/${card.dataset.id}/autobaud`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ port: f.port.value }),
    })).json();
    if (r.baud) f.baud.value = r.baud;
    auto.textContent = "AUTO";
    document.getElementById("config-status").textContent = r.cfg_error || (r.baud ? `locked ${r.baud}` : "no baud found");
  };
  const baudRow = field("Baud", f.baud); baudRow.append(auto);
  card.append(hd, field("Label", f.label), field("Input", f.mode), field("Serial port", f.port),
    baudRow, field("Color", f.color), field("Pos X mm", f.posx), field("Pos Y mm", f.posy),
    field("Heading °", f.head), field("Min range m", f.min), field("Max range m", f.max),
    field("Speed sens", f.sens));
  card._read = () => ({
    id: card.dataset.id, label: f.label.value, input_mode: f.mode.value, serial_port: f.port.value,
    serial_baud: +f.baud.value, color: f.color.value, position: [+f.posx.value, +f.posy.value],
    heading: +f.head.value, min_range_mm: (+f.min.value) * 1000, max_range_mm: (+f.max.value) * 1000,
    speed_sensitivity: +f.sens.value, sim_pattern: s.sim_pattern || "circle", sim_hz: s.sim_hz || 10,
    broker: s.broker, port: s.port, topic: s.topic, trail_on: s.trail_on ?? true, enabled: s.enabled ?? true,
  });
  return card;
}
function globalSlider(parent, label, val, min, max, step) {
  const row = document.createElement("div"); row.className = "globalrow";
  const l = document.createElement("label"); l.textContent = label;
  const out = document.createElement("span"); out.textContent = val;
  const inp = mkInput("range", val, { min, max, step }); inp.oninput = () => (out.textContent = inp.value);
  row.append(l, inp, out); parent.append(row); return inp;
}
async function openConfig() {
  const cfg = await (await fetch("/api/config")).json();
  state.globals = cfg.globals || {}; state.night = !!state.globals.night;
  state.trailDuration = +(state.globals.trail_duration ?? 5.0); refreshToggles();
  const list = document.getElementById("sensor-list"); list.innerHTML = "";
  for (const s of cfg.sensors) list.appendChild(sensorCard(s));
  const g = document.getElementById("config-globals"); g.innerHTML = "";
  const add = document.createElement("button"); add.textContent = "+ ADD SENSOR";
  add.onclick = () => { const id = nextSensorId(); list.appendChild(sensorCard({ id, label: id, color: "#00ddff", input_mode: "sim", position: [2000, 2000], heading: 0, min_range_mm: 0, max_range_mm: 8000 })); };
  g.append(add);
  state._gSmooth = globalSlider(g, "Smoothing level", state.globals.smooth_level ?? 0, 0, 9, 1);
  state._gTrail = globalSlider(g, "Trail seconds", state.globals.trail_duration ?? 5, 1, 20, 0.5);
  document.getElementById("config").hidden = false;
}
document.getElementById("config-x").onclick = () => (document.getElementById("config").hidden = true);
document.getElementById("config-apply").onclick = async () => {
  const sensors = [...document.querySelectorAll(".sensor-card")].map((c) => c._read());
  const patch = { sensors, smooth_level: +state._gSmooth.value, trail_duration: +state._gTrail.value };
  const r = await (await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) })).json();
  state.trailDuration = patch.trail_duration;
  document.getElementById("config-status").textContent = r.cfg_error || "applied ✓";
};

// ── PLAYBACK drawer ────────────────────────────────────────────────────────────────────────
async function openPlayback() {
  const { tracks } = await (await fetch("/api/tracks")).json();
  const list = document.getElementById("track-list"); list.innerHTML = "";
  for (const t of tracks.slice(0, 40)) {
    const div = document.createElement("div"); div.className = "track"; div.dataset.id = t.id;
    div.textContent = `${t.sensor_id}/${t.serial}  ${(t.first_seen || "").slice(0, 19)}  pts=${t.point_count ?? "?"}`;
    div.onclick = () => loadTrack(t.id, div);
    list.appendChild(div);
  }
  document.getElementById("playback").hidden = false;
}
async function loadTrack(id, div) {
  const d = await (await fetch(`/api/tracks/${id}`)).json();
  const pts = d.points.map((p) => ({ t: p.t_rel_s ?? 0, x: p.x_mm, y: p.y_mm, v: p.speed_mm_s }));
  state.playback = { track: d.track, points: pts, start: performance.now() / 1000, playing: true, idx: 0 };
  // highlight the playing track (persists, like the hover style) + raise the bottom transport
  document.querySelectorAll("#track-list .track").forEach((el) => el.classList.toggle("active", el === div));
  document.getElementById("pb-bar").hidden = false;
  document.getElementById("pb-play").textContent = "⏸";
}
function stopPlayback() {
  state.playback = null;
  document.getElementById("pb-bar").hidden = true;
  document.querySelectorAll("#track-list .track").forEach((el) => el.classList.remove("active"));
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
  state.pbClock = (wall ? wall.toLocaleString("en-GB") + "   " : "") + `(+${(pb.t || 0).toFixed(1)}s)`;
  const clk = document.getElementById("pb-clock"); if (clk) clk.textContent = state.pbClock;
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
document.getElementById("playback-x").onclick = () => { stopPlayback(); document.getElementById("playback").hidden = true; };
document.getElementById("pb-close").onclick = stopPlayback;
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
