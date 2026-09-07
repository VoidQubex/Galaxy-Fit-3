"""
Actions & quick-reply buttons for the Galaxy Fit 3 server.

CONFIGURATION lives in config.json (copy config.example.json). This file holds
only the Python: helper functions and the custom functions that config.json can
reference by name. server.py reads the four structures built at the bottom
(ACTIONS, QUICK_REPLIES, QUICK_REPLY_ACTIONS, DEBOUNCE) - it never needs editing.

config.json entries pick ONE action type:
  * launch : an app name or absolute path, started detached (like double-click)
  * shell  : a raw command line, run detached
  * run    : the name of a function registered below with @register(...)

To add custom behaviour: write a function fn(ctx) here, decorate it with
@register("name"), then reference "name" from config.json via "run": "name".

ctx passed to a run-function includes:
  * ctx['notify'](title, body="", app_name="PC", alert=True, popup=True)
      push a notification BACK to the band (thread-safe). See fetch_homeassistant.
  * for quick-reply taps: ctx['text'] (the button label), ctx['seq'].
Do slow work (network, disk) on a thread so it doesn't stall the server.
"""

import datetime
import json
import os
import random
import shutil
import subprocess
import threading
import winreg

import keyboard

import homeassistant
import input_control
import sap


# ---- function registry --------------------------------------------------------
# config.json's  "run": "name"  resolves to the function registered under "name".

ACTION_FUNCTIONS = {}


def register(name):
    def deco(fn):
        ACTION_FUNCTIONS[name] = fn
        return fn
    return deco


# ---- predicate registry -------------------------------------------------------
# config.json's  "when": { "predicate": "name" }  resolves to a function
# registered here that returns a bool. Predicates are evaluated every time the
# launcher (re)arms, so keep them FAST / non-blocking (no network - cache instead).

PREDICATES = {}


def predicate(name):
    def deco(fn):
        PREDICATES[name] = fn
        return fn
    return deco


@predicate("")
def pred_is_work_hours(ctx):
    """Example predicate: true 09:00-18:00 on weekdays."""
    now = datetime.datetime.now()
    return now.weekday() < 5 and 9 <= now.hour < 18


# ---- custom action functions (reference these from config.json via "run") -----

@register("notification")
def action_notification(ctx):
    """Show a Windows notification."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        "$n.ShowBalloonTip(5000, 'Galaxy Fit 3', 'Something happened!', 'Info');"
        "Start-Sleep -Seconds 6; $n.Dispose()"
    )
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps])

@register("media_playpause")
def action_media_playpause(ctx):
    """Send the media virtual keys to Windows."""
    key_code = ctx['key_code']
    if key_code == sap.KEY_PLAYPAUSE:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             "$w = New-Object -ComObject WScript.Shell; $w.SendKeys([char]179)"],
        )
    elif key_code == sap.KEY_NEXT:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             "$w = New-Object -ComObject WScript.Shell; $w.SendKeys([char]176)"],
        )

    elif key_code == sap.KEY_PREVIOUS:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             "$w = New-Object -ComObject WScript.Shell; $w.SendKeys([char]177)"],
        )


@register("action_center")
def action_action_center(ctx):
    """Open the Windows Action Center (notification tray)."""
    keyboard.press_and_release('windows+a')


# ---- mouse / keyboard remote control ------------------------------------------
# These take arguments from config.json via "args", e.g.
#   { "label": "Up",  "run": "mouse_move", "args": { "dy": -40 } }
#   { "label": "a",   "run": "type",       "args": { "text": "a" } }
#   { "label": "Copy","run": "key",        "args": { "key": "c", "mods": ["ctrl"] } }

@register("mouse_move")
def action_mouse_move(ctx):
    a = ctx.get("args", {})
    input_control.mouse_move(a.get("dx", 0), a.get("dy", 0))


@register("mouse_click")
def action_mouse_click(ctx):
    a = ctx.get("args", {})
    input_control.mouse_click(a.get("button", "left"), bool(a.get("double", False)))


@register("mouse_scroll")
def action_mouse_scroll(ctx):
    a = ctx.get("args", {})
    input_control.mouse_scroll(a.get("amount", 1), bool(a.get("horizontal", False)))


@register("key")
def action_key(ctx):
    """Tap a key by VK name (see input_control.VK) or a single character, with
    optional modifiers: {"key": "c", "mods": ["ctrl"]}."""
    a = ctx.get("args", {})
    name = a.get("key")
    mods = a.get("mods") or []
    if not name:
        return
    if len(str(name)) == 1 and str(name).lower() not in input_control.VK:
        # A bare character with modifiers needs its VK; letters/digits map directly.
        vk = ord(str(name).upper())
        input_control.key_press(vk, mods)
    else:
        input_control.key_press(name, mods)


@register("type")
def action_type(ctx):
    """Type literal text: {"text": "hello"}. Layout-independent."""
    text = ctx.get("args", {}).get("text", "")
    if text:
        input_control.type_text(text)


@register("fetch_homeassistant")
def action_fetch_homeassistant(ctx):
    """Fetch live data from Home Assistant and push it back to the band as a
    notification. The HTTP call runs off-thread so it never stalls the server;
    ctx['notify'] (added by the server) delivers the result to the wrist."""
    notify = ctx.get("notify")
    if notify is None:
        print("[fetch] no notify callback in ctx - server too old?")
        return

    def work():
        title, body = homeassistant.summary()   # never raises; errors come back as text
        notify(title, body, app_name="Home Assistant")

    threading.Thread(target=work, daemon=True).start()


# ---- app launch helper (used by "launch" entries) -----------------------------
# Fully detached: no console, no process group shared with the server. Without
# this, a launched process can attach to the SAME console window the server is
# running in - so closing/crashing that app can take the server down with it.
_DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def _resolve_app_path(name: str):
    """Resolve a Win+R-style app name to its absolute path, via the same "App
    Paths" registry lookup Explorer/os.startfile use internally, falling back to
    a PATH search. Returns None if nothing was found."""
    if os.path.isabs(name) and os.path.exists(name):
        return name
    candidates = [name] if os.path.splitext(name)[1] else [name + ".exe"]
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for exe in candidates:
            try:
                key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
                with winreg.OpenKey(hive, key) as k:
                    path, _ = winreg.QueryValueEx(k, None)   # default value
                    if path and os.path.exists(path):
                        return path
            except FileNotFoundError:
                continue
    for exe in candidates + [name]:
        found = shutil.which(exe)
        if found:
            return found
    return None


def _launch(name: str):
    """Return an action that launches an app the way double-clicking its .exe
    would: resolved to an absolute path, started fully detached (see _DETACHED).
    Batch/cmd shims (e.g. VS Code's "code" launcher) aren't real executables -
    CreateProcess can't run them directly - so those go through os.startfile
    instead, same as a real double-click would."""
    def run(ctx):
        path = _resolve_app_path(name)
        if path is None:
            os.startfile(name)   # last resort: let Explorer's own resolution try
            return
        if os.path.splitext(path)[1].lower() in (".cmd", ".bat"):
            os.startfile(path)
            return
        subprocess.Popen([path], cwd=os.path.dirname(path) or None,
                         creationflags=_DETACHED, close_fds=True)
    return run


# ---- config loader ------------------------------------------------------------
# Builds the four structures server.py consumes from config.json. One bad entry
# is skipped with a warning; a missing/invalid file degrades to "no buttons".

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_CONFIG_PATH = CONFIG_PATH


# ---- "when" conditions (context-sensitive quick replies) ----------------------
# A quick-reply entry may carry an optional "when" object with EXACTLY ONE
# condition type. The button is shown only when its condition is currently true;
# no "when" = always shown. Conditions are re-evaluated every launcher arm, so a
# button can appear/vanish by time of day, chance, a custom predicate, etc.
# Add a new condition type by registering an evaluator in CONDITION_TYPES.

def _cond_predicate(value, where):
    """when: { "predicate": "name" }  -> a bool-returning function in PREDICATES."""
    fn = PREDICATES.get(value)
    if fn is None:
        print(f"[config] {where}: unknown predicate {value!r} "
              f"(registered: {', '.join(sorted(PREDICATES)) or 'none'}) - button hidden")
        return lambda ctx: False

    def check(ctx):
        try:
            return bool(fn(ctx))
        except Exception as e:  # noqa: BLE001 - a broken predicate hides, never crashes
            print(f"[config] {where}: predicate {value!r} errored ({e}) - button hidden")
            return False
    return check


def _parse_hhmm(s):
    h, m = str(s).split(":")
    return datetime.time(int(h), int(m))


def _cond_time(value, where):
    """when: { "time": { "from": "09:00", "to": "17:00" } }  (wraps past midnight)."""
    try:
        start, end = _parse_hhmm(value["from"]), _parse_hhmm(value["to"])
    except Exception as e:  # noqa: BLE001
        print(f"[config] {where}: bad time condition ({e}) - button hidden")
        return lambda ctx: False

    def check(ctx):
        now = datetime.datetime.now().time()
        return start <= now <= end if start <= end else (now >= start or now <= end)
    return check


def _cond_random(value, where):
    """when: { "random": 0.3 }  -> shown with 30% probability each arm."""
    try:
        p = float(value)
    except Exception as e:  # noqa: BLE001
        print(f"[config] {where}: bad random probability ({e}) - button hidden")
        return lambda ctx: False
    return lambda ctx: random.random() < p


CONDITION_TYPES = {
    "predicate": _cond_predicate,
    "time": _cond_time,
    "random": _cond_random,
}


def _compile_condition(when, where):
    """Compile a 'when' object into callable(ctx)->bool, or None for 'always show'."""
    if when is None:
        return None
    if not isinstance(when, dict):
        print(f"[config] {where}: 'when' must be an object - button hidden")
        return lambda ctx: False
    kinds = [k for k in CONDITION_TYPES if k in when]
    if len(kinds) != 1:
        found = ", ".join(kinds) if kinds else "none"
        print(f"[config] {where}: 'when' needs exactly one of "
              f"{'/'.join(CONDITION_TYPES)} (got {found}) - button hidden")
        return lambda ctx: False
    kind = kinds[0]
    return CONDITION_TYPES[kind](when[kind], where)


def _and_conditions(*conds):
    """Combine conditions with AND. None means 'always'; Nones are dropped. So a
    button inside a `when` group with its own `when` shows only when both hold."""
    active = [c for c in conds if c is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    return lambda ctx: all(c(ctx) for c in active)


def _resolve_action(spec: dict, where: str):
    """Turn one config entry into a callable-or-string server._run_action handles,
    or None (with a warning) if the entry is malformed."""
    kinds = [k for k in ("launch", "shell", "run", "menu") if k in spec]
    if len(kinds) != 1:
        found = ", ".join(kinds) if kinds else "none"
        print(f"[config] {where}: need exactly one of launch/shell/run/menu "
              f"(got {found}) - skipped")
        return None
    kind = kinds[0]
    value = spec[kind]
    if kind == "menu":
        return ("__menu__", str(value))     # navigation, handled by the server
    if kind == "launch":
        return _launch(value)
    if kind == "shell":
        return value                       # string -> server runs it detached
    fn = ACTION_FUNCTIONS.get(value)        # kind == "run"
    if fn is None:
        print(f"[config] {where}: unknown run function {value!r} "
              f"(registered: {', '.join(sorted(ACTION_FUNCTIONS))}) - skipped")
        return None
    args = spec.get("args")
    if args is None:
        return fn
    # Bind config args into ctx so one registered function serves many buttons
    # (26 letters shouldn't need 26 functions).
    def bound(ctx, _fn=fn, _args=args):
        ctx = dict(ctx)
        ctx["args"] = _args
        return _fn(ctx)
    bound.__name__ = f"{value}{tuple(sorted(args.items()))}"
    return bound


def _walk_quick_replies(entries, parent_cond, where, out_entries, out_actions):
    """Flatten a (possibly nested) quick_replies tree into (label, condition) pairs.

    An entry is a GROUP if it has "buttons" - its optional "when" applies to all
    of them and groups can nest; otherwise it's a BUTTON (label + action + optional
    own "when"). A button's effective condition is the AND of every enclosing
    group's condition and its own, so the whole tree collapses to the same flat
    (label, condition) list the rest of the code already consumes.
    """
    for i, entry in enumerate(entries):
        here = f"{where}[{i}]"
        if isinstance(entry, dict) and "buttons" in entry:
            group_cond = _compile_condition(entry.get("when"), f"{here} (group)")
            combined = _and_conditions(parent_cond, group_cond)
            _walk_quick_replies(entry["buttons"], combined, f"{here}.buttons",
                                out_entries, out_actions)
            continue
        label = entry.get("label") if isinstance(entry, dict) else None
        if not label:
            print(f"[config] {here}: not a group (no 'buttons') and no 'label' - skipped")
            continue
        action = _resolve_action(entry, f"{here} ({label})")
        if action is None:
            continue
        own_cond = _compile_condition(entry.get("when"), f"{here} ({label})")
        out_entries.append((label, _and_conditions(parent_cond, own_cond)))
        out_actions[label] = action


def _read_config_file():
    """Load and parse config.json. Returns None if missing or invalid."""
    if not os.path.exists(_CONFIG_PATH):
        print("[config] config.json not found - no buttons/actions loaded. "
              "Copy config.example.json to config.json to configure.")
        return None
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001 - a bad file must not crash the server
        print(f"[config] config.json is invalid ({e}) - no buttons/actions loaded.")
        return None


def _parse_actions_and_quick_replies(cfg):
    """Return (ACTIONS, QUICK_REPLY_ENTRIES, QUICK_REPLY_ACTIONS, DEBOUNCE).

    QUICK_REPLY_ENTRIES is a list of (label, condition) where condition is
    callable(ctx)->bool or None ("always show").
    """
    actions, quick_reply_entries, quick_reply_actions, debounce = {}, [], {}, {}

    # Quick-reply buttons - list/tree order is the button order on the band.
    # Entries may be plain buttons or `when` groups holding their own buttons.
    _walk_quick_replies(cfg.get("quick_replies", []), None, "quick_replies",
                        quick_reply_entries, quick_reply_actions)

    # Channel/msg remaps.
    for i, entry in enumerate(cfg.get("actions", [])):
        try:
            ch = entry["channel"]
            ch = int(ch, 0) if isinstance(ch, str) else int(ch)
            msg = int(entry["msg"])
        except (KeyError, ValueError, TypeError) as e:
            print(f"[config] actions[{i}]: bad channel/msg ({e}) - skipped")
            continue
        action = _resolve_action(entry, f"actions[{i}] (0x{ch:02x}/{msg})")
        if action is None:
            continue
        key = (ch, msg)
        actions[key] = action
        window = entry.get("debounce", 0)
        if window:
            debounce[key] = float(window)

    return actions, quick_reply_entries, quick_reply_actions, debounce


def _load_config():
    cfg = _read_config_file()
    if cfg is None:
        return {}, [], {}, {}
    return _parse_actions_and_quick_replies(cfg)


def _empty_menus():
    return {"": {"entries": [], "actions": {}, "page_size": DEFAULT_PAGE_SIZE, "pages": None}}


def _apply_config(cfg):
    """Refresh module-level config structures from a parsed config dict."""
    global ACTIONS, QUICK_REPLY_ENTRIES, QUICK_REPLY_ACTIONS, DEBOUNCE, QUICK_REPLIES, MENUS
    if cfg is None:
        ACTIONS, QUICK_REPLY_ENTRIES, QUICK_REPLY_ACTIONS, DEBOUNCE = {}, [], {}, {}
        QUICK_REPLIES = []
        MENUS = _empty_menus()
        return
    ACTIONS, QUICK_REPLY_ENTRIES, QUICK_REPLY_ACTIONS, DEBOUNCE = _parse_actions_and_quick_replies(cfg)
    QUICK_REPLIES = [label for label, _ in QUICK_REPLY_ENTRIES]
    MENUS = _load_menus(cfg)


def reload_config():
    """Re-read config.json and refresh module-level structures in place.

    Returns True on success. On a missing/invalid file, leaves the previous
    config in place and returns False.
    """
    cfg = _read_config_file()
    if cfg is None:
        return False
    _apply_config(cfg)
    return True


# ---- menus & paging -----------------------------------------------------------
# A menu is a named screen of buttons. "" is the root (the `quick_replies` list).
# A button with "menu": "name" navigates instead of running something; the server
# swaps the band's quick-reply list and re-arms the launcher, which is what makes
# nested screens (mouse pad, keyboard pages) possible.
#
# Menus longer than their page_size use an index → leaf layout instead of
# Prev/Next scrolling:
#   * page == PAGE_INDEX (-1): group labels summarizing each leaf (e.g. "a-c")
#     plus any config "Back" buttons (up to the parent menu)
#   * page >= 0: one leaf of buttons, with PAGE_BACK at the end (→ index)
# That keeps jumps to any leaf within a couple of taps.

PAGE_INDEX = -1
PAGE_BACK = "Back"          # injected on leaf pages; returns to the index
DEFAULT_PAGE_SIZE = 9
_SUMMARY_MAX = 8            # band button text is short; truncate ends of ranges


def _load_menus(cfg):
    """Return {name: {"entries", "actions", "page_size", "pages"}}.

    `pages` is None for auto-chunked menus, or a list of
    {"title": str, "entries": [(label, cond), ...]} when the config uses labeled
    page groups: { "label": "Nudge", "buttons": [ ... ] }.
    A group with `buttons` but no `label` is still a conditional flatten (when).
    """
    menus = {}
    root_entries, root_actions = [], {}
    _walk_quick_replies(cfg.get("quick_replies", []), None, "quick_replies",
                        root_entries, root_actions)
    menus[""] = {"entries": root_entries, "actions": root_actions,
                 "page_size": int(cfg.get("page_size", DEFAULT_PAGE_SIZE)),
                 "pages": None}

    for name, spec in (cfg.get("menus") or {}).items():
        if not isinstance(spec, dict):
            print(f"[config] menus.{name}: must be an object - skipped")
            continue
        entries, acts, pages = _load_menu_buttons(
            spec.get("buttons", []), f"menus.{name}.buttons")
        menus[str(name)] = {
            "entries": entries, "actions": acts, "pages": pages,
            "page_size": int(spec.get("page_size", cfg.get("page_size", DEFAULT_PAGE_SIZE))),
        }
    return menus


def _load_menu_buttons(buttons, where):
    """Walk one menu's buttons; detect labeled page groups vs flat lists.

    Returns (entries, actions, pages) where pages is None (auto-chunk) or a list
    of {"title", "entries"} for explicit index leaves. Duplicate labels (e.g.
    Click on two pages) are allowed - both show; actions keep one mapping.
    """
    entries, acts, pages = [], {}, []
    loose = []  # top-level buttons outside any labeled page
    for i, entry in enumerate(buttons or []):
        here = f"{where}[{i}]"
        if not isinstance(entry, dict):
            print(f"[config] {here}: expected an object - skipped")
            continue
        if "buttons" in entry and entry.get("label"):
            page_entries, page_acts = [], {}
            group_cond = _compile_condition(entry.get("when"), f"{here} (page)")
            _walk_quick_replies(entry["buttons"], group_cond, f"{here}.buttons",
                                page_entries, page_acts)
            pages.append({"title": str(entry["label"]), "entries": page_entries})
            entries.extend(page_entries)
            acts.update(page_acts)
            continue
        if "buttons" in entry:
            # Conditional group (no label) - flatten into the loose list.
            group_cond = _compile_condition(entry.get("when"), f"{here} (group)")
            _walk_quick_replies(entry["buttons"], group_cond, f"{here}.buttons",
                                loose, acts)
            continue
        # Single top-level button (typically Back).
        label = entry.get("label")
        if not label:
            print(f"[config] {here}: not a page/group and no 'label' - skipped")
            continue
        action = _resolve_action(entry, f"{here} ({label})")
        if action is None:
            continue
        own_cond = _compile_condition(entry.get("when"), f"{here} ({label})")
        item = (label, own_cond)
        loose.append(item)
        acts[label] = action

    if pages:
        # Explicit pages: keep Back (and any other nav) as top-level entries only.
        for label, cond in loose:
            entries.append((label, cond))
            if label != PAGE_BACK:
                print(f"[config] {where}: loose button {label!r} with labeled "
                      f"pages - shown on the index only via entries; prefer Back "
                      f"or put it inside a page")
        return entries, acts, pages

    # Fully flat menu - loose holds everything.
    return loose, acts, None


def menu_exists(menu: str) -> bool:
    return str(menu) in MENUS


def _short(label: str, n: int = _SUMMARY_MAX) -> str:
    return label if len(label) <= n else label[: max(1, n - 2)] + ".."


def _summarize_chunk(chunk):
    """Human-readable range label for an index button, e.g. 'a-c' or 'Copy-Undo'."""
    if not chunk:
        return "?"
    if len(chunk) == 1:
        return _short(chunk[0])
    return f"{_short(chunk[0])}-{_short(chunk[-1])}"


def _is_parent_back(menu: str, label: str) -> bool:
    """True for config Back buttons that navigate to another menu (usually root)."""
    action = menu_action(menu, label)
    return isinstance(action, tuple) and action and action[0] == "__menu__" and label == PAGE_BACK


def _menu_visible(menu: str, ctx=None):
    """Split visible labels into content vs parent-Back nav for `menu`."""
    ctx = ctx or {}
    m = MENUS.get(str(menu))
    if m is None:
        return [], [], 9
    visible = [label for label, cond in m["entries"] if cond is None or cond(ctx)]
    content, backs = [], []
    for label in visible:
        if _is_parent_back(menu, label):
            backs.append(label)
        else:
            content.append(label)
    return content, backs, max(1, m["page_size"])


def _menu_chunks(menu: str, ctx=None):
    """Content chunks for a paged menu, or None if everything fits on one screen.

    Returns (chunks, content, backs, size, titles).
      * chunks is None → flat (no index)
      * titles is None → auto summaries from chunk contents
      * titles is a list → explicit page-group labels (same length as chunks)
    """
    ctx = ctx or {}
    m = MENUS.get(str(menu))
    if m is None:
        return None, [], [], 9, None

    size = max(1, m["page_size"])
    backs = [label for label, cond in m["entries"]
             if _is_parent_back(menu, label) and (cond is None or cond(ctx))]

    if m.get("pages"):
        chunks, titles = [], []
        for page in m["pages"]:
            labels = [label for label, cond in page["entries"]
                      if cond is None or cond(ctx)]
            if not labels:
                continue
            # Leaf must leave one slot for PAGE_BACK; trim with a warning if needed.
            per = max(1, size - 1)
            if len(labels) > per:
                print(f"[config] menu {menu!r} page {page['title']!r}: "
                      f"{len(labels)} buttons > {per} (page_size {size} - Back) "
                      f"- truncating")
                labels = labels[:per]
            chunks.append(labels)
            titles.append(page["title"])
        if not chunks:
            return None, [], backs, size, None
        if len(chunks) == 1 and len(chunks[0]) + len(backs) <= size:
            # Single explicit page that fits with Back → show flat (no index).
            return None, chunks[0], backs, size, None
        content = [label for chunk in chunks for label in chunk]
        return chunks, content, backs, size, titles

    content, backs, size = _menu_visible(menu, ctx)
    if len(content) + len(backs) <= size:
        return None, content, backs, size, None
    per = max(1, size - 1)
    chunks = [content[i:i + per] for i in range(0, len(content), per)]
    return chunks, content, backs, size, None


def _unique_summaries(chunks, titles=None):
    """Build index labels; use explicit titles when provided."""
    if titles is not None:
        summaries, seen = [], {}
        for title in titles:
            base = _short(title)
            n = seen.get(base, 0)
            seen[base] = n + 1
            summaries.append(base if n == 0 else f"{base} ({n + 1})")
        return summaries
    summaries, seen = [], {}
    for chunk in chunks:
        base = _summarize_chunk(chunk)
        n = seen.get(base, 0)
        seen[base] = n + 1
        summaries.append(base if n == 0 else f"{base} ({n + 1})")
    return summaries


def menu_start_page(menu: str = "", ctx=None) -> int:
    """Page to show when entering a menu: index if it pages, else 0."""
    chunks, _, _, _, _ = _menu_chunks(menu, ctx)
    return PAGE_INDEX if chunks else 0


def menu_labels(menu: str = "", page: int = 0, ctx=None):
    """Labels to show for `menu` at `page`, honouring 'when' and index/leaf paging.

    Returns (labels, page, total_leaf_pages).
      * total_leaf_pages == 1 → flat menu (no index); page is 0
      * total_leaf_pages  > 1 → page PAGE_INDEX is the group index; page 0..N-1
        are leaves (each ending with PAGE_BACK)
    """
    chunks, content, backs, size, titles = _menu_chunks(menu, ctx)
    if chunks is None:
        return content + backs, 0, 1

    total = len(chunks)
    summaries = _unique_summaries(chunks, titles)

    if page == PAGE_INDEX or page < 0:
        budget = max(0, size - len(backs))
        labels = summaries[:budget] + backs
        return labels, PAGE_INDEX, total

    page = page % total
    return list(chunks[page]) + [PAGE_BACK], page, total


def menu_index_target(menu: str, label: str, ctx=None):
    """If `label` is an index group for this menu, return its leaf page; else None."""
    chunks, _, _, _, titles = _menu_chunks(menu, ctx)
    if not chunks:
        return None
    summaries = _unique_summaries(chunks, titles)
    try:
        return summaries.index(label)
    except ValueError:
        return None


def menu_action(menu: str, label: str):
    """Look the action up WITHIN a menu, so two menus can both have a 'Back'."""
    m = MENUS.get(str(menu))
    if m is None:
        return None
    return m["actions"].get(label)


# Built at import; call reload_config() to hot-swap after editing config.json.
# server.py reads ACTIONS / QUICK_REPLIES / QUICK_REPLY_ACTIONS / DEBOUNCE,
# plus menu_labels()/menu_action() for the menu-aware button list.
ACTIONS = {}
QUICK_REPLY_ENTRIES = []
QUICK_REPLY_ACTIONS = {}
DEBOUNCE = {}
QUICK_REPLIES = []
MENUS = _empty_menus()
_apply_config(_read_config_file())


def current_quick_replies(ctx=None):
    """Root-menu labels (back-compat shim for callers that don't do menus)."""
    labels, _, _ = menu_labels("", menu_start_page("", ctx), ctx)
    return labels
