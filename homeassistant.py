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
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "ha_config.json")

# Last successful IPv4 rewrite for a hostname. mDNS (.local) is flaky on
# Windows; caching avoids three separate DNS lookups for three entities.
_IPV4_CACHE: dict[str, str] = {}


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


def _prefer_ipv4_url(url: str, retries: int = 3) -> str:
    """Rewrite host to an IPv4 literal when DNS is flaky (common with .local).

    mDNS often returns an IPv6 link-local address first; urllib then fails with
    getaddrinfo / unreachable. Prefer a plain IPv4 address when available, and
    reuse a cached rewrite so a multi-entity fetch doesn't re-resolve thrice.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host or host[0].isdigit() or ":" in host:
        return url
    cached = _IPV4_CACHE.get(host)
    if cached:
        return cached
    infos = []
    for attempt in range(retries):
        try:
            infos = socket.getaddrinfo(host, parts.port or 80, socket.AF_INET,
                                       socket.SOCK_STREAM)
            if infos:
                break
        except OSError:
            if attempt + 1 < retries:
                time.sleep(0.3)
    if not infos:
        return url
    ip = infos[0][4][0]
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    rewritten = urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    _IPV4_CACHE[host] = rewritten
    return rewritten


def get_state(entity_id: str, url: str, token: str, timeout: float = 8.0) -> dict:
    """Raw state dict for one entity (keys: state, attributes, ...)."""
    parts = urllib.parse.urlsplit(url)
    host_header = parts.netloc
    last_err = None
    for attempt in range(3):
        base = _prefer_ipv4_url(url)
        req = urllib.request.Request(
            f"{base}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Host": host_header})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # noqa: BLE001 - retry transient DNS/connect flaps
            last_err = e
            # Drop a bad cache entry so the next attempt re-resolves.
            if parts.hostname in _IPV4_CACHE:
                del _IPV4_CACHE[parts.hostname]
            time.sleep(0.3)
    raise last_err


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
