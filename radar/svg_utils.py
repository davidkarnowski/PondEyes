"""
radar.svg_utils
===============

Raster-to-Pygame helpers for the mini-radar project.

*   Always scales an SVG in **contain** mode (never crops).
*   We keep `trim_alpha()` for future use, but **do not call it** by
    default; leaving the original transparent border preserves the map’s
    coordinate origin so millimetre → pixel math stays valid.
"""

from io import BytesIO
import xml.etree.ElementTree as ET
import pygame, cairosvg


# ────────── internal helpers ──────────
def _svg_mm(path: str) -> tuple[float, float]:
    """Return (width_mm, height_mm) declared in the `<svg>` element."""
    root = ET.parse(path).getroot()

    def mm(v: str) -> float:
        if v.endswith("mm"):
            return float(v[:-2])
        if v.endswith("cm"):
            return float(v[:-2]) * 10
        return float(v)

    w, h = root.get("width"), root.get("height")
    if w and h:                          # explicit width/height attributes
        return mm(w), mm(h)
    _, _, w, h = map(float, root.get("viewBox").split())
    return w, h


def _raster_svg(path: str, ppm: float) -> pygame.Surface:
    """Render SVG at *pixels-per-mm* and return a Pygame RGBA surface."""
    w_mm, h_mm = _svg_mm(path)
    w_px, h_px = int(w_mm * ppm), int(h_mm * ppm)
    png = cairosvg.svg2png(url=path, output_width=w_px, output_height=h_px)
    return pygame.image.load(BytesIO(png)).convert_alpha()


def trim_alpha(surf: pygame.Surface) -> pygame.Surface:
    """
    Return a copy cropped to non-transparent pixels.
    **Not used by default** because it shifts the origin.
    """
    return surf.subsurface(surf.get_bounding_rect()).copy()


# ────────── public API ──────────
def fit_svg(path: str, box_size: tuple[int, int]) -> tuple[pygame.Surface, float]:
    """
    Rasterise *path* so the result is fully contained in `box_size`
    (width_px, height_px).  Returns `(surface, ppm_final)` where
    `ppm_final` is the effective pixels-per-millimetre after scaling.

    Implementation notes:
    ---------------------
    1.  Choose an initial `ppm0` so the *untrimmed* SVG fits into the box.
    2.  Rasterise once with CairoSVG at that dpi.
    3.  Optionally **do not** trim; we keep the origin aligned.
    4.  A second `scale` (<= 1) may be needed if the raster still overflows
        by a pixel due to rounding; apply with `smoothscale`.
    5.  **Final ppm** = `ppm0 * scale` (multiply, *not* divide!).
    """
    sw, sh = box_size
    mm_w, mm_h = _svg_mm(path)
    ppm0 = min(sw / mm_w, sh / mm_h)            # initial guess – contain

    raw = _raster_svg(path, ppm0)               # no trim_alpha()
    rw, rh = raw.get_width(), raw.get_height()

    scale = min(sw / rw, sh / rh)               # second fit if needed
    surf = pygame.transform.smoothscale(
        raw, (int(rw * scale), int(rh * scale))
    )

    ppm_final = ppm0 * scale                    # ← **correct** formula
    return surf, ppm_final
