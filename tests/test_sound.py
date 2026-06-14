# tests.test_sound
# ================
#
# Locks in the B1 Raspberry-Pi contract: when no audio device is available the import-time
# mixer.init() is guarded (radar/sound.py), so the app must NOT crash — beep() degrades to a
# silent no-op whose .play() does nothing. The call site `beep(freq).play()` must stay valid.

from radar import sound


def test_beep_is_silent_noop_when_audio_unavailable(monkeypatch):
    # Simulate a Pi with no audio device: force AVAILABLE False and confirm beep() returns a
    # stand-in whose .play() is a harmless no-op (so gui.py's `beep(freq).play()` never throws).
    monkeypatch.setattr(sound, "AVAILABLE", False)
    snd = sound.beep(1000)
    assert snd.play() is None        # no exception, no real playback


def test_beep_returns_playable_when_audio_available():
    # On a normal host (the macOS dev box / CI with a device) the mixer is up: beep() returns a
    # cached pygame Sound that has a callable play(). If audio happens to be unavailable in this
    # environment, the guard above is the contract that matters, so we skip rather than fail.
    if not sound.AVAILABLE:
        import pytest
        pytest.skip("no audio device in this environment")
    snd = sound.beep(1000)
    assert hasattr(snd, "play") and callable(snd.play)
