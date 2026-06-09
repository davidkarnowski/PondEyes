// webapp/geometry.js
// ==================
// The COSMETIC projection only. The authoritative radar geometry (sensor-local -> world-mm via
// local_to_world) is done server-side in radar/geometry.py; the browser receives WORLD-mm and
// just scales it to canvas pixels + pans/zooms. We mirror radar.geometry's fov_arc / sensor_fill
// math (pure trig) so the cones/gates/rings match the pygame view exactly.

export const FOV_DEG = 120;

// A view: fits the map's mm extent into the canvas, then applies a zoom + pan (pure viewport).
export class View {
  constructor() {
    this.mapW = 1; this.mapH = 1;          // map extent in mm
    this.basePpm = 1; this.baseOffX = 0; this.baseOffY = 0;   // fit-to-canvas baseline
    this.zoom = 1; this.panX = 0; this.panY = 0;
  }
  // Recompute the fit baseline for a canvas size (contain, centered). Call on resize / meta load.
  fit(canvasW, canvasH, mapW_mm, mapH_mm) {
    this.mapW = mapW_mm; this.mapH = mapH_mm;
    this.basePpm = Math.min(canvasW / mapW_mm, canvasH / mapH_mm);
    this.baseOffX = (canvasW - mapW_mm * this.basePpm) / 2;
    this.baseOffY = (canvasH - mapH_mm * this.basePpm) / 2;
  }
  get ppm() { return this.basePpm * this.zoom; }
  get offX() { return this.baseOffX * this.zoom + this.panX; }
  get offY() { return this.baseOffY * this.zoom + this.panY; }

  // World-mm -> canvas px. World y is UP (origin bottom-left of the map), matching the pygame app:
  // py = off + (mapH - my) * ppm.
  worldToPx(mx, my) {
    return [this.offX + mx * this.ppm, this.offY + (this.mapH - my) * this.ppm];
  }
  pxToWorld(px, py) {
    return [(px - this.offX) / this.ppm, this.mapH - (py - this.offY) / this.ppm];
  }
  // Zoom about a canvas focus point, keeping the world point under it fixed.
  zoomAbout(factor, fx, fy, maxZoom = 8) {
    const nz = Math.max(1, Math.min(maxZoom, this.zoom * factor));
    if (nz === this.zoom) return;
    const [wx, wy] = this.pxToWorld(fx, fy);
    this.zoom = nz;
    // solve pan so worldToPx(wx,wy) == (fx,fy) at the new zoom
    this.panX = fx - this.baseOffX * this.zoom - wx * this.ppm;
    this.panY = fy - this.baseOffY * this.zoom - (this.mapH - wy) * this.ppm;
  }
  panBy(dx, dy) { this.panX += dx; this.panY += dy; }
  reset() { this.zoom = 1; this.panX = 0; this.panY = 0; }
}

// FOV arc points in CANVAS px around a sensor at (sx,sy) px. heading 0 = up (screen -y), matching
// radar.geometry.fov_arc_pts: pt = (sx + sin(th)*R, sy - cos(th)*R).
export function fovArcPts(sx, sy, headingDeg, R_px, fovDeg = FOV_DEG, steps = 32) {
  const pts = [];
  const half = (fovDeg / 2) * Math.PI / 180;
  const th0 = headingDeg * Math.PI / 180;
  for (let i = 0; i <= steps; i++) {
    const a = th0 - half + (2 * half) * (i / steps);
    pts.push([sx + Math.sin(a) * R_px, sy - Math.cos(a) * R_px]);
  }
  return pts;
}

// Filled wedge (pie if no near gate, annulus if there is) between R_in and R_out px.
export function sensorFillPts(sx, sy, headingDeg, R_out, R_in, fovDeg = FOV_DEG) {
  if (R_out <= R_in) return null;
  const outer = fovArcPts(sx, sy, headingDeg, R_out, fovDeg);
  if (R_in <= 0) return [[sx, sy], ...outer];                 // pie slice (apex at sensor)
  const inner = fovArcPts(sx, sy, headingDeg, R_in, fovDeg).reverse();
  return [...outer, ...inner];                                 // annulus
}
