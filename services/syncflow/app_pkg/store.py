"""SyncFlow — data layer: the demo profile record, its version,
delta history and event stream, persisted to a JSON file in
SYNCFLOW_DATA_DIR. Moved verbatim from the original app.py: same
schema, same seed, same save-via-temp-file semantics."""
import json
import threading
from datetime import datetime, timezone

from .config import DATA_DIR, STATE_FILE

PROFILE_FIELDS = ("name", "display_name", "bio", "location", "timezone")

DEMO_PROFILE = {
    "name": "Alex Demo",
    "display_name": "alex.demo",
    "bio": "Sample profile for the SyncFlow demo - edit me on either device.",
    "location": "Demo City",
    "timezone": "UTC",
}

lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_state():
    return {
        "profile": dict(DEMO_PROFILE),
        "version": 1,
        "deltas": [],
        "events": [],
        "next_seq": 1,
    }


def _load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if (
            isinstance(state.get("profile"), dict)
            and isinstance(state.get("version"), int)
            and isinstance(state.get("deltas"), list)
            and isinstance(state.get("events"), list)
            and isinstance(state.get("next_seq"), int)
        ):
            return state
    except Exception:
        pass
    return _seed_state()


def _save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def emit(state, type_, version, detail):
    state["events"].append(
        {
            "seq": state["next_seq"],
            "type": type_,
            "version": version,
            "timestamp": _now(),
            "detail": detail,
        }
    )
    state["next_seq"] += 1


_state = _load_state()


def current():
    return _state


def save():
    _save_state(_state)


def reset():
    global _state
    _state = _seed_state()
    _save_state(_state)
    return _state


def now():
    return _now()
