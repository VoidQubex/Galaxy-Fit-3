"""
Windows "now playing" reader.

Pulls the current media session (whatever app owns the system media transport
controls: Spotify, Firefox, VLC, ...) via WinRT so the band can display real
track info instead of placeholder data.

Requires: pip install winrt-Windows.Media.Control
"""

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

# Sent when nothing is playing, so the band always gets a well-formed reply
# instead of hanging waiting for one.
IDLE = {
    "title": "Nothing playing",
    "artist": "",
    "app": "",
    "playing": False,
    "position_ms": 0,
    "duration_ms": 0,
}

_manager = None


async def _get_manager():
    global _manager
    if _manager is None:
        _manager = await SessionManager.request_async()
    return _manager


def _ms(timespan) -> int:
    """WinRT TimeSpan -> milliseconds (winrt maps it to datetime.timedelta)."""
    if timespan is None:
        return 0
    try:
        return max(0, int(timespan.total_seconds() * 1000))
    except AttributeError:
        return 0


async def get_media_state() -> dict:
    """Current now-playing state, or IDLE if there's no active session.

    Never raises - a media read must not be able to take the server down.
    """
    try:
        manager = await _get_manager()
        session = manager.get_current_session()
        if session is None:
            return dict(IDLE)

        props = await session.try_get_media_properties_async()
        info = session.get_playback_info()
        timeline = session.get_timeline_properties()

        playing = info.playback_status == PlaybackStatus.PLAYING
        return {
            "title": props.title or "",
            "artist": props.artist or "",
            "app": session.source_app_user_model_id or "",
            "playing": playing,
            "position_ms": _ms(timeline.position),
            "duration_ms": _ms(timeline.end_time),
        }
    except Exception:  # noqa: BLE001 - fall back rather than kill the server
        return dict(IDLE)


def summary(state: dict) -> str:
    t = state.get("title") or "?"
    a = state.get("artist") or ""
    who = f"{t} - {a}" if a else t
    return f"{who} [{'playing' if state.get('playing') else 'paused'}]"
