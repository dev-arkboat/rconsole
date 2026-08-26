"""User authentication and persistence (default user: ark / ark)."""
import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
USERS_FILE = DATA_DIR / "users.json"

# A static pepper so hashes are not plain SHA256 of the password.
PEPPER = os.environ.get("RCONSOLE_PEPPER", "rconsole-static-pepper-change-me")


def _hash(password):
    return hashlib.sha256((password + PEPPER).encode("utf-8")).hexdigest()


def _load():
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(users):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


def seed_defaults():
    users = _load()
    if "ark" not in users:
        users["ark"] = {"password": _hash("ark"), "is_admin": True}
        _save(users)


def reset_to_defaults():
    """Factory reset: wipe all users and recreate the default ark account."""
    users = {"ark": {"password": _hash("ark"), "is_admin": True}}
    _save(users)


def verify(username, password):
    users = _load()
    u = users.get(username)
    if not u:
        return False
    return u["password"] == _hash(password)


def get_user(username):
    return _load().get(username)


def list_users():
    return list(_load().keys())


def create_user(username, password, is_admin=False):
    users = _load()
    if username in users:
        return False, "User already exists"
    if not username or not username.isalnum():
        return False, "Username must be alphanumeric"
    if len(password) < 4:
        return False, "Password must be at least 4 characters"
    users[username] = {"password": _hash(password), "is_admin": is_admin}
    _save(users)
    return True, None


def set_password(username, password):
    users = _load()
    if username not in users:
        return False, "No such user"
    if len(password) < 4:
        return False, "Password must be at least 4 characters"
    users[username]["password"] = _hash(password)
    _save(users)
    return True, None


def set_username(old, new):
    users = _load()
    if old not in users:
        return False, "No such user"
    if new in users:
        return False, "Username already taken"
    if not new or not new.isalnum():
        return False, "Username must be alphanumeric"
    users[new] = users.pop(old)
    _save(users)
    return True, None


def set_admin(username, is_admin):
    users = _load()
    if username not in users:
        return False, "No such user"
    users[username]["is_admin"] = bool(is_admin)
    _save(users)
    return True, None


def delete_user(username):
    users = _load()
    if username not in users:
        return False, "No such user"
    users.pop(username)
    _save(users)
    return True, None
