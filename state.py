"""Server-side state for rconsole: per-user tabs and hosted apps.

State is keyed by **username** (a stable identity), not the ephemeral browser
session, so a user who logs out and back in -- or who returns days later -- is
restored to exactly where they left off. Tab metadata is persisted to disk so it
survives a server restart; long-running terminal sessions are re-spawned on
login when their process is no longer alive.
"""
import atexit
import json
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
STATE_DIR = DATA / "state"

STATE = {}  # username -> {"tabs": {...}, "hosts": {...}}
STATE_LOCK = threading.Lock()

# Persistence is debounced: per-command writes (history/cwd) only mark a user
# dirty and a background thread flushes them at most every few seconds, so a
# request thread never blocks on disk I/O. Structural changes call save_now().
_dirty = set()
_flush_lock = threading.Lock()
_flusher_started = False


def default_cwd():
    """Real working directory where host (passthrough) commands run."""
    return str(HERE)


# --------------------------------------------------------------------------- io
def _state_path(username):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in username if c.isalnum() or c in "._-")
    return STATE_DIR / f"{safe}.json"


def _write(username):
    sess = STATE.get(username)
    if not sess:
        return
    try:
        with open(_state_path(username), "w", encoding="utf-8") as f:
            json.dump({
                "tabs": {tid: _tab_persist(t) for tid, t in sess["tabs"].items()}
            }, f)
    except Exception:
        pass


def _flush_loop():
    while True:
        time.sleep(2)
        with _flush_lock:
            users = list(_dirty)
            _dirty.clear()
        for u in users:
            try:
                _write(u)
            except Exception:
                pass


def _ensure_flusher():
    global _flusher_started
    if _flusher_started:
        return
    _flusher_started = True
    threading.Thread(target=_flush_loop, daemon=True).start()


def save(username):
    """Mark state dirty; flushed to disk asynchronously (debounced)."""
    with _flush_lock:
        _dirty.add(username)
    _ensure_flusher()


def save_now(username):
    """Persist immediately (structural changes that must survive a restart)."""
    _write(username)


def flush_all():
    with _flush_lock:
        users = list(_dirty)
        _dirty.clear()
    for u in users:
        try:
            _write(u)
        except Exception:
            pass


atexit.register(flush_all)


def _tab_persist(t):
    return {
        "id": t.get("id"),
        "name": t.get("name"),
        "cwd": t.get("cwd", default_cwd()),
        "isTerminal": bool(t.get("isTerminal", False)),
        "cmd": t.get("cmd", ""),
        "history": (t.get("history") or [])[-200:],
        "gen": t.get("gen"),
        "prompt": t.get("prompt"),
    }


def _load(username):
    try:
        with open(_state_path(username), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    tabs = {}
    for tid, t in (data.get("tabs") or {}).items():
        tabs[tid] = {
            "id": t.get("id", tid),
            "name": t.get("name", tid),
            "cwd": t.get("cwd", default_cwd()),
            "isTerminal": bool(t.get("isTerminal", False)),
            "cmd": t.get("cmd", ""),
            "history": t.get("history", []),
            "gen": t.get("gen"),
            "prompt": t.get("prompt"),
        }
    return {"tabs": tabs, "hosts": {}}


# ------------------------------------------------------------------------- session
def get_session(username):
    return STATE.get(username)


def new_session(username):
    with STATE_LOCK:
        if username in STATE:
            return STATE[username]
        sess = _load(username) or {"tabs": {}, "hosts": {}}
        STATE[username] = sess
        return sess


def ensure_tab(sess, username, tab_id):
    if tab_id not in sess["tabs"]:
        sess["tabs"][tab_id] = {
            "id": tab_id,
            "name": tab_id,
            "cwd": default_cwd(),
            "isTerminal": False,
            "cmd": "",
            "history": [],
            "gen": None,
            "prompt": None,
        }
        save_now(username)
    return sess["tabs"][tab_id]


def set_terminal(sess, username, tab_id, cmd, cwd):
    """Mark a tab as a persistent terminal and persist it."""
    tab = ensure_tab(sess, username, tab_id)
    tab["isTerminal"] = True
    tab["cmd"] = cmd
    tab["cwd"] = cwd
    save_now(username)
    return tab


def remove_tab(username, tab_id):
    sess = STATE.get(username)
    if sess and tab_id in sess["tabs"]:
        sess["tabs"].pop(tab_id, None)
        save_now(username)


# --------------------------------------------------------------------------- hosts
def kill_host(username, folder, port):
    key = (folder, port)
    with STATE_LOCK:
        sess = STATE.get(username)
        if sess and key in sess["hosts"]:
            proc = sess["hosts"].pop(key)
            _terminate(proc)
        ACTIVE_HOSTS.pop(key, None)


def kill_all_hosts(username):
    sess = STATE.get(username)
    if not sess:
        return
    for key in list(sess["hosts"].keys()):
        folder, port = key
        kill_host(username, folder, port)


def register_host(username, folder, port, proc):
    key = (folder, port)
    with STATE_LOCK:
        sess = STATE.get(username)
        if sess:
            sess["hosts"][key] = proc
        ACTIVE_HOSTS[key] = {
            "url": f"http://127.0.0.1:{port}",
            "proc": proc,
            "sid": username,
        }
    return key


# Global registry of live hosted apps so the proxy route can forward traffic.
ACTIVE_HOSTS = {}

HOST_LOCK = threading.Lock()


def _terminate(proc):
    try:
        proc.terminate()
        def _kill():
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        threading.Thread(target=_kill, daemon=True).start()
    except Exception:
        pass
