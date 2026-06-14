# radar.sound
# ===========
#
# Tiny sine-wave beep cache so we don't synthesise a tone on the mixer every frame.
#
# Pi note: pygame.mixer.init() is attempted ONCE at import. On a headless Pi (or any host with
# no configured audio device) it raises pygame.error — so we guard it. When the mixer is
# unavailable the app stays fully functional and just runs SILENT: beep() returns a no-op stand-in
# whose .play() does nothing, so the call site `beep(freq).play()` needs no change. Setting
# SDL_AUDIODRIVER=dummy makes init() succeed silently instead (AVAILABLE stays True, no real sound).

import math
from array import array
import pygame

from radar.logging_setup import get_logger

log = get_logger("sound")

# Try to bring up the mixer. If there's no audio device, degrade to silence instead of crashing
# the whole pygame app at import time (which is what happens on a Pi with audio unconfigured).
AVAILABLE = False
try:
    # Buffer 2048 (was 512): a larger mixer buffer means fewer underruns, which is the main cause of
    # the "crackle" on the Pi's PWM headphone jack. (The jack's analog noise floor is hardware; this
    # cleans up the playback itself.)
    pygame.mixer.pre_init(44100, -16, 1, 2048)
    pygame.mixer.init()
    AVAILABLE = True
except pygame.error as exc:
    log.warning("audio mixer unavailable (%s) — beeps disabled", exc)

_cache = {}


# A stand-in returned when audio is unavailable: its .play() is a no-op, so every existing
# `beep(...).play()` call keeps working without a None check at the call site.
class _Silent:
    def play(self, *a, **k):
        return None


_SILENT = _Silent()


def beep(freq, dur=0.08, vol=0.5, sr=44100):
    if not AVAILABLE:
        return _SILENT                       # no audio device: silent no-op
    key = (freq, dur)
    if key not in _cache:
        # Apply a short attack/release envelope so the tone fades in and out instead of starting and
        # stopping on a hard sample edge — that hard edge is exactly what makes a raw beep "click"/
        # "pop". With the envelope + the bigger mixer buffer the beep is clean.
        n   = int(dur * sr)
        atk = max(1, int(0.005 * sr))            # 5 ms fade-in
        rel = max(1, int(0.012 * sr))            # 12 ms fade-out
        def _env(i):
            if i < atk:     return i / atk
            if i > n - rel: return max(0.0, (n - i) / rel)
            return 1.0
        buf = array(
            "h",
            (int(vol * 32767 * _env(i) * math.sin(2 * math.pi * freq * i / sr))
             for i in range(n)),
        )
        s = pygame.mixer.Sound(buffer=buf.tobytes())
        s.set_volume(vol)
        _cache[key] = s
    return _cache[key]
