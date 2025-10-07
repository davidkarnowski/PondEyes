"""Tiny sine-wave beep cache so we don't hit the mixer every frame."""
import math
from array import array
import pygame

pygame.mixer.pre_init(44100, -16, 1, 512)
pygame.mixer.init()
_cache = {}

def beep(freq, dur=0.08, vol=0.5, sr=44100):
    key = (freq, dur)
    if key not in _cache:
        buf = array(
            "h",
            (int(vol * 32767 * math.sin(2 * math.pi * freq * i / sr))
             for i in range(int(dur * sr))),
        )
        s = pygame.mixer.Sound(buffer=buf.tobytes())
        s.set_volume(vol)
        _cache[key] = s
    return _cache[key]

