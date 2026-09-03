"""
Windows notification reader.

Uses UserNotificationListener to see toasts from other apps (Discord, mail, ...)
so the server can forward them to the band, with each app's real icon.

Requires: pip install winrt-Windows.UI.Notifications.Management
Note: Windows asks the user to allow this once (Settings > Privacy > Notifications).
"""

import io

from winrt.windows.foundation import Size
from winrt.windows.storage.streams import DataReader
from winrt.windows.ui.notifications import NotificationKinds
from winrt.windows.ui.notifications.management import (
    UserNotificationListener,
    UserNotificationListenerAccessStatus,
)

_listener = None


async def request_access() -> bool:
    """Ask Windows for notification-read access. False if denied/unavailable."""
    global _listener
    try:
        L = UserNotificationListener.current
        status = await L.request_access_async()
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            return False
        _listener = L
        return True
    except Exception:  # noqa: BLE001 - unsupported OS build, etc.
        return False


async def _read_logo(display_info) -> bytes:
    """The app's logo as PNG bytes, or b'' if it has none."""
    try:
        ref = display_info.get_logo(Size(96, 96))
        if ref is None:
            return b""
        stream = await ref.open_read_async()
        reader = DataReader(stream)
        await reader.load_async(stream.size)
        buf = bytearray(stream.size)
        reader.read_bytes(buf)
        return bytes(buf)
    except Exception:  # noqa: BLE001
        return b""


async def get_notifications() -> list:
    """Current toast notifications as dicts.

    Keys: id, app, key (stable per-app id), title, body, logo_png (may be b'').
    Never raises - a bad notification is skipped, not fatal.
    """
    if _listener is None:
        return []
    try:
        items = await _listener.get_notifications_async(NotificationKinds.TOAST)
    except Exception:  # noqa: BLE001
        return []

    out = []
    for n in list(items):
        try:
            nid = n.id
        except Exception:  # noqa: BLE001
            continue

        app, key, logo = "Unknown", "unknown.app", b""
        try:
            info = n.app_info
            di = info.display_info
            app = di.display_name or "Unknown"
            # Prefer the AUMID as the stable key; fall back to the display name.
            try:
                key = info.app_user_model_id or app
            except Exception:  # noqa: BLE001
                key = app
            logo = await _read_logo(di)
        except Exception:  # noqa: BLE001 - some system toasts have no app_info
            pass

        title, body = "", ""
        try:
            binding = n.notification.visual.get_binding("ToastGeneric")
            if binding is not None:
                texts = [t.text for t in binding.get_text_elements()]
                if texts:
                    title = texts[0] or ""
                    body = " ".join(t for t in texts[1:] if t)
        except Exception:  # noqa: BLE001
            pass

        if not title and not body:
            continue    # nothing worth showing on a wrist

        out.append({"id": nid, "app": app, "key": key,
                    "title": title, "body": body, "logo_png": logo})
    return out


def remove_notification(notification_id: int) -> bool:
    """Dismiss a Windows notification (mirrors clearing it on the band)."""
    if _listener is None:
        return False
    try:
        _listener.remove_notification(notification_id)
        return True
    except Exception:  # noqa: BLE001 - already gone, or not permitted
        return False


def logo_to_image(png_bytes: bytes):
    """PNG bytes -> PIL Image, or None."""
    if not png_bytes:
        return None
    try:
        from PIL import Image
        return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
