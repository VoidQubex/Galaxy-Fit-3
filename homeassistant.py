"""
Home Assistant fetcher.

Pulls entity states from your Home Assistant instance over its REST API so the
band can show live data (temperatures, sensors, whatever you configure).

Config: create `ha_config.json` next to this file (copy ha_config.example.json),
or set the HA_URL / HA_TOKEN environment variables. The long-lived access token
is a credential - keep ha_config.json local, don't commit or share it.

Stdlib only (urllib) - no extra dependencies.
"""

import json
import os
import urllib.error
import urllib.request

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "ha_config.json")


def _load_config() -> dict:
    """Merge ha_config.json with HA_URL / HA_TOKEN env overrides."""
    cfg = {"url": None, "token": None, "title": "Home Assistant", "entities": []}
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # noqa: BLE001 - report, don't crash the caller
            raise RuntimeError(f"ha_config.json is invalid: {e}")
    cfg["url"] = os.environ.get("HA_URL", cfg["url"])
    cfg["token"] = os.environ.get("HA_TOKEN", cfg["token"])
    if cfg["url"]:
        cfg["url"] = cfg["url"].rstrip("/")
    return cfg


def get_state(entity_id: str, url: str, token: str, timeout: float = 8.0) -> dict:
    """Raw state dict for one entity (keys: state, attributes, ...)."""
    req = urllib.request.Request(
        f"{url}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _format_entity(entity: dict, url: str, token: str) -> str:
    """One display line for a configured entity, e.g. 'Living Room: 21.4 °C'."""
    eid = entity.get("entity_id", "")
    label = entity.get("label")
    try:
        s = get_state(eid, url, token)
    except urllib.error.HTTPError as e:
        return f"{label or eid}: HTTP {e.code}"
    except Exception:  # noqa: BLE001 - one bad entity shouldn't sink the rest
        return f"{label or eid}: unreachable"
    attrs = s.get("attributes", {}) or {}
    name = label or attrs.get("friendly_name") or eid
    state = s.get("state", "?")
    unit = attrs.get("unit_of_measurement")
    return f"{name}: {state} {unit}".strip() if unit else f"{name}: {state}"


def summary():
    """(title, body) ready to push as a band notification.

    Never raises - any failure comes back as a readable body instead, so the
    action that calls this can always show *something* on the wrist.
    """
    try:
        cfg = _load_config()
    except RuntimeError as e:
        return "Home Assistant", str(e)

    if not cfg["url"] or not cfg["token"]:
        return ("Home Assistant",
                "Not configured. Copy ha_config.example.json to ha_config.json "
                "and fill in url + token.")
    if not cfg["entities"]:
        return cfg.get("title", "Home Assistant"), "No entities configured."

    lines = [_format_entity(e, cfg["url"], cfg["token"]) for e in cfg["entities"]]
    return cfg.get("title", "Home Assistant"), "\n".join(lines)


if __name__ == "__main__":
    t, b = summary()
    print(t)
    print(b)
