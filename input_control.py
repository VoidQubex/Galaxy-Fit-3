"""
Mouse + keyboard injection for the band's remote-control menus.

Uses Win32 SendInput (not keybd_event/mouse_event) because SendInput is what
modern apps, browsers and games actually respect.

Text is sent with KEYEVENTF_UNICODE, so any character types correctly regardless
of the active keyboard layout - no virtual-key mapping needed for letters.
"""

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)

# INPUT types
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# MOUSEINPUT flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

# KEYBDINPUT flags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

# Common virtual-key codes, addressable from config by name.
VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "apps": 0x5D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "volup": 0xAF, "voldown": 0xAE, "mute": 0xAD,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(*inputs):
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    sent = _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def _mouse(flags, dx=0, dy=0, data=0):
    return _INPUT(type=INPUT_MOUSE,
                  u=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, data, flags, 0, None)))


def _key(vk=0, scan=0, flags=0):
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, None)))


# ---- mouse -------------------------------------------------------------------

def mouse_move(dx: int, dy: int):
    """Relative cursor move, in pixels.

    NOTE: Windows applies pointer acceleration / "enhance pointer precision" to
    relative moves, so the cursor travels somewhat further than the numbers given
    (measured: a 40,25 request moved 50,31). Step sizes are therefore approximate
    - tune the config's step values to taste rather than expecting exact pixels.
    """
    _send(_mouse(MOUSEEVENTF_MOVE, int(dx), int(dy)))


def mouse_click(button: str = "left", double: bool = False):
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    _send(_mouse(down), _mouse(up))
    if double:
        _send(_mouse(down), _mouse(up))


def mouse_scroll(amount: int = 1, horizontal: bool = False):
    """Scroll by `amount` notches (positive = up / right)."""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send(_mouse(flag, data=int(amount) * WHEEL_DELTA))


# ---- keyboard ----------------------------------------------------------------

def key_press(name_or_vk, modifiers=None):
    """Tap a virtual key by VK name (see VK) or numeric code, with optional
    modifiers like ["ctrl", "shift"] held down around it."""
    vk = VK.get(str(name_or_vk).lower(), name_or_vk) if not isinstance(name_or_vk, int) \
        else name_or_vk
    if not isinstance(vk, int):
        raise ValueError(f"unknown key {name_or_vk!r}")
    mods = [VK[m.lower()] for m in (modifiers or []) if m.lower() in VK]
    seq = [_key(vk=m) for m in mods]
    seq += [_key(vk=vk), _key(vk=vk, flags=KEYEVENTF_KEYUP)]
    seq += [_key(vk=m, flags=KEYEVENTF_KEYUP) for m in reversed(mods)]
    _send(*seq)


def type_text(text: str):
    """Type a literal string. Uses UNICODE scancodes, so it is layout-independent
    and handles any character (including emoji-free punctuation, accents, etc.)."""
    seq = []
    for ch in str(text):
        code = ord(ch)
        # Characters outside the BMP need a surrogate pair; handle both.
        units = [code] if code <= 0xFFFF else [
            0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        for u in units:
            seq.append(_key(scan=u, flags=KEYEVENTF_UNICODE))
            seq.append(_key(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if seq:
        # SendInput takes a bounded array; chunk long strings.
        for i in range(0, len(seq), 64):
            _send(*seq[i:i + 64])
