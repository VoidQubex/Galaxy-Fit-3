"""
Windows master-volume control, for the band's volume commands.

Uses pycaw (Core Audio) so we can do absolute set + mute, not just up/down
key taps. Falls back to media-key presses if pycaw isn't usable.

Requires: pip install pycaw comtypes
"""

import ctypes

# The band speaks Android's media-volume scale: 0..BAND_MAX notches, not percent.
# (Announcing maxVolume=100 didn't change that - a full slider drag still sent
# ~17 steps, so its UI uses its own scale.) Work in band units and convert, so a
# full drag maps to the full 0-100% range instead of nudging by a few percent.
BAND_MAX = 15


def pct_to_units(pct: int) -> int:
    return max(0, min(BAND_MAX, round(pct * BAND_MAX / 100.0)))


def units_to_pct(units: int) -> int:
    return max(0, min(100, round(units * 100.0 / BAND_MAX)))

_user32 = ctypes.windll.user32
_KEYUP = 0x0002
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF


def _tap(vk):
    _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(vk, 0, _KEYUP, 0)


def _endpoint():
    """The default playback device's volume interface, or None."""
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetSpeakers().EndpointVolume
    except Exception:  # noqa: BLE001 - pycaw missing / COM unavailable
        return None


def get_volume() -> int:
    """Current master volume 0-100, or -1 if unknown."""
    v = _endpoint()
    if v is None:
        return -1
    try:
        return round(v.GetMasterVolumeLevelScalar() * 100)
    except Exception:  # noqa: BLE001
        return -1


def is_muted() -> bool:
    v = _endpoint()
    try:
        return bool(v.GetMute()) if v is not None else False
    except Exception:  # noqa: BLE001
        return False


def set_volume(percent: int) -> bool:
    v = _endpoint()
    if v is None:
        return False
    try:
        v.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
        return True
    except Exception:  # noqa: BLE001
        return False


def set_mute(muted: bool) -> bool:
    v = _endpoint()
    if v is None:
        _tap(VK_VOLUME_MUTE)     # key can only toggle, but better than nothing
        return False
    try:
        v.SetMute(1 if muted else 0, None)
        return True
    except Exception:  # noqa: BLE001
        return False


def step(up: bool) -> int:
    """Move one BAND notch (1/15th of the range). Returns the new percent, or -1."""
    cur = get_volume()
    if cur < 0:
        _tap(VK_VOLUME_UP if up else VK_VOLUME_DOWN)
        return -1
    units = pct_to_units(cur) + (1 if up else -1)
    new = units_to_pct(units)
    set_volume(new)
    return new


def get_units() -> int:
    """Current volume in band units (0..BAND_MAX), 0 when muted."""
    if is_muted():
        return 0
    v = get_volume()
    return pct_to_units(v) if v >= 0 else 0
