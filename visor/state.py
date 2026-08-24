"""What we have already looked at, and when.

The loop must not re-analyse an item forever. Posting a comment bumps the
item's updatedAt, so if we stored nothing after posting we would see our own
comment as new activity and answer ourselves indefinitely. The rule is
therefore: record the item's updatedAt *after* we finish with it, including
whatever our own comment did to it.
"""
import datetime as dt
import json
import os
from pathlib import Path

STATE_FILE = Path(
    os.environ.get("VISOR_STATE_FILE", Path.home() / ".ai-visor" / "processed.json")
)

VERSION = 1


def _empty():
    """A genuinely fresh state.

    Must not be a shared constant copied with dict(): that is shallow, so every
    caller would end up mutating the same nested `items` dict.
    """
    return {"version": VERSION, "items": {}}


def load():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return _empty()
    return data


def save(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)  # atomic; a crash mid-write cannot corrupt state


def _key(repo, number):
    return f"{repo or 'local'}#{number}"


def record(repo, number, kind, updated_at, sha=None, state=None):
    """Mark an item analysed as of `updated_at`."""
    state = state if state is not None else load()
    entry = state["items"].get(_key(repo, number), {})
    state["items"][_key(repo, number)] = {
        "kind": kind,
        "updated_at": updated_at,
        "sha": sha,
        "analyzed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "count": entry.get("count", 0) + 1,
    }
    save(state)
    return state


def needs_analysis(repo, number, updated_at, sha=None, state=None):
    """True when the item has changed since we last analysed it.

    `updated_at` comes straight from GitHub and moves on any activity: new
    comment, edit, label, or push. For PRs, a changed head SHA also counts, so
    a force-push with an unchanged timestamp is still caught.
    """
    state = state if state is not None else load()
    entry = state["items"].get(_key(repo, number))
    if entry is None:
        return True
    if sha and entry.get("sha") != sha:
        return True
    return str(updated_at) != str(entry.get("updated_at"))


def seen(repo, number, state=None):
    state = state if state is not None else load()
    return state["items"].get(_key(repo, number))


def forget(repo, number):
    """Drop an item so it will be analysed again next pass."""
    state = load()
    if state["items"].pop(_key(repo, number), None) is not None:
        save(state)
        return True
    return False
