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

pygame.font.init()
FONT       = pygame.font.SysFont("monospace", 18)
MID_FONT   = pygame.font.SysFont("monospace", 10)
SMALL_FONT = pygame.font.SysFont("monospace", 14)
TRACK_FONT = pygame.font.SysFont("monospace", 32)
BIG_FONT   = pygame.font.SysFont("monospace", 48)

ASCII_SURFS = [MID_FONT.render(l, True, GREEN)
               for l in ASCII_BANNER.splitlines()]
ASCII_H     = sum(s.get_height() for s in ASCII_SURFS)

HEADER_GAP   = 10                       # gap under ASCII + menu
MENU_H       = FONT.get_height()
TOP_PAD_N    = ASCII_H + MENU_H + HEADER_GAP
BOTTOM_PAD_N = 140

# -------- MAP mode --------
MAP_BORDER = 5

# -------- dirs --------
ROOT      = Path(__file__).resolve().parent.parent
LOG_DIR   = ROOT / "log"
CFG_PATH  = ROOT / "radar_config.json"

