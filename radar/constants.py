"""
Hard-coded colours, paddings & fonts so every module can import them
without circular dependencies.
"""
from pathlib import Path
import pygame

# -------- colours --------
GREEN, DIM, BLACK, RED = (0, 255, 0), (0, 90, 0), (0, 0, 0), (255, 0, 0)
GRADIENT = [(0, 255, 0), (0, 170, 255), (255, 170, 0), (255, 0, 0)]

# -------- layout (NORMAL mode) --------
ASCII_BANNER = """
.__ .__..  ..__   .___.   ,.___ __.
[__)|  ||\ ||  \••[__  \./ [__ (__ 
|   |__|| \||__/••[___  |  [___.__)
""".strip("\n")

# -------- fonts + font-derived layout (REBUILDABLE for responsive sizing) --------
# All sizes here are the desktop (scale=1.0) baseline. apply_scale() rebuilds the fonts and the
# layout values derived from them and republishes them to this module's globals. Because every
# call-site reads `C.FONT`/`C.SMALL_FONT`/... (module attributes), a single reassignment rescales the
# whole UI with NO per-site edits. The GUI calls apply_scale() once at startup with a factor from the
# real screen size; apply_scale(1.0) runs at import so default/desktop behaviour is unchanged.

pygame.font.init()

_FONT_PT       = {"FONT": 18, "MID_FONT": 10, "SMALL_FONT": 14, "TRACK_FONT": 32, "BIG_FONT": 48}
_HEADER_GAP_PT = 10
_BOTTOM_PAD_PT = 140
_MIN_PT        = 7                       # legibility floor — never render text below this

# Bound at import by apply_scale(1.0); re-bound by the GUI once it knows the screen size.
FONT = MID_FONT = SMALL_FONT = TRACK_FONT = BIG_FONT = None
ASCII_SURFS = []
ASCII_H = MENU_H = HEADER_GAP = TOP_PAD_N = BOTTOM_PAD_N = 0
UI_SCALE = 1.0


def apply_scale(scale=1.0):
    # Rebuild every font + the layout values derived from them at `scale`, and publish to module
    # globals. Idempotent; safe to call repeatedly (one GUI per process).
    global FONT, MID_FONT, SMALL_FONT, TRACK_FONT, BIG_FONT, ASCII_SURFS, ASCII_H
    global MENU_H, HEADER_GAP, TOP_PAD_N, BOTTOM_PAD_N, UI_SCALE
    UI_SCALE = scale
    def _f(pt):
        return pygame.font.SysFont("monospace", max(_MIN_PT, round(pt * scale)))
    FONT       = _f(_FONT_PT["FONT"])
    MID_FONT   = _f(_FONT_PT["MID_FONT"])
    SMALL_FONT = _f(_FONT_PT["SMALL_FONT"])
    TRACK_FONT = _f(_FONT_PT["TRACK_FONT"])
    BIG_FONT   = _f(_FONT_PT["BIG_FONT"])
    ASCII_SURFS = [MID_FONT.render(l, True, GREEN) for l in ASCII_BANNER.splitlines()]
    ASCII_H     = sum(s.get_height() for s in ASCII_SURFS)
    HEADER_GAP  = max(4, round(_HEADER_GAP_PT * scale))   # gap under ASCII + menu
    MENU_H      = FONT.get_height()
    TOP_PAD_N   = ASCII_H + MENU_H + HEADER_GAP
    BOTTOM_PAD_N = max(60, round(_BOTTOM_PAD_PT * scale))


apply_scale(1.0)                        # desktop baseline at import

# -------- MAP mode --------
MAP_BORDER = 5

# -------- dirs --------
ROOT      = Path(__file__).resolve().parent.parent
LOG_DIR   = ROOT / "log"
CFG_PATH  = ROOT / "radar_config.json"

