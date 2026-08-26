"""srconsole — a Flask-powered simulated Linux console with hosted apps."""
import os
import secrets
import time
import uuid
from datetime import timedelta

import requests
from flask import (Flask, Response, abort, redirect, render_template,
                   request, session, url_for)
from flask_sock import Sock

import auth
import interp
import ptyhost
import state
import termsess

app = Flask(__name__)
app.secret_key = os.environ.get("RCONSOLE_SECRET", secrets.token_hex(32))
# Long-lived sessions: a user should be able to return days later and log back
# in to the exact same state. Forced logout only clears the cookie; server-side
# state is keyed by username and survives it.
app.permanent_session_lifetime = timedelta(days=30)

SESSION_TIMEOUT = 30 * 24 * 3600  # seconds of inactivity before forced re-login
HOSTS = state.ACTIVE_HOSTS
sock = Sock(app)


def restore_terminal_sessions(username):
    """Re-spawn terminal tabs whose process is no longer alive (e.g. after a
    server restart). Skipped while the process is already running, so an active
    session is never duplicated."""
    sess = state.get_session(username)
    if not sess:
        return
    for tid, t in sess["tabs"].items():
        if t.get("isTerminal") and t.get("cmd"):
            if termsess.get(username, tid) is None:
                try:
                    s = termsess.TermSession(username, tid, t["cmd"], t["cwd"])
                    with termsess.SESSIONS_LOCK:
                        termsess.SESSIONS[(username, tid)] = s
                except Exception:
                    pass


@app.before_request
def guard():
    # Allow login page and static assets without a session.
    if request.endpoint in ("login", "static"):
        return
    if "username" not in session:
        if request.endpoint in ("console_page", "run", "proxy"):
            if request.endpoint == "run" or request.path.startswith("/api"):
                return {"error": "unauthorized"}, 401
            return redirect(url_for("login"))
        return
    # Enforce inactivity timeout (only forces a re-login; user state persists).
    last = session.get("last_activity", 0)
    import time
    if time.time() - float(last) > SESSION_TIMEOUT:
        session.clear()
        return redirect(url_for("login"))
    session["last_activity"] = time.time()

    # If the account no longer exists (e.g. after a global rboot), force login.
    if auth.get_user(session["username"]) is None:
        session.clear()
        return redirect(url_for("login"))

    # Ensure server-side session state exists, then bring any persisted terminal
    # sessions (that aren't already running) back to life.
    username = session["username"]
    if username not in state.STATE:
        state.new_session(username)
    restore_terminal_sessions(username)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if auth.verify(username, password):
            session.clear()
            session["username"] = username
            session["login_time"] = __import__("time").time()
            session["last_activity"] = __import__("time").time()
            session.permanent = True
            # Keyed by username: a returning user is restored to their old state.
            state.new_session(username)
            restore_terminal_sessions(username)
            return redirect(url_for("console_page"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, default_user="ark")


@app.route("/")
@app.route("/console")
def console_page():
    if "username" not in session:
        return redirect(url_for("login"))
    return render_template("console.html", username=session["username"])


@app.route("/api/run", methods=["POST"])
def run():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    raw = data.get("cmd", "")
    tab_id = data.get("tab", "main")

    username = session["username"]
    sess = state.get_session(username)
    tab = state.ensure_tab(sess, username, tab_id)
    u = auth.get_user(username)
    is_admin = bool(u and u.get("is_admin"))

    # Record history only for real commands (not interactive prompt replies).
    if tab.get("gen") is None and raw.strip():
        tab["history"].append(raw)
        if len(tab["history"]) > 500:
            tab["history"] = tab["history"][-500:]
        state.save(username)

    result = interp.process(tab, username, is_admin, username, raw)
    if isinstance(result, dict):
        result["cwd"] = tab["cwd"]
    else:
        result = {"output": result, "cwd": tab["cwd"]}

    # Apply session updates requested by a command (e.g. cuser).
    if isinstance(result, dict) and result.get("update_session"):
        for k, v in result["update_session"].items():
            session[k] = v

    if isinstance(result, dict) and result.get("action") == "logout":
        # Logout only clears the cookie. Terminal sessions are keyed by username
        # and deliberately kept alive so the user can log back in and resume.
        session.clear()
        result["redirect"] = url_for("login")
    elif isinstance(result, dict) and result.get("action") == "rboot":
        termsess.kill_all_for(username)
        # A global reboot wipes every session (commands.py clears STATE), so
        # tear down all terminal sessions across users as well.
        with termsess.SESSIONS_LOCK:
            all_keys = list(termsess.SESSIONS.keys())
        for k in all_keys:
            try:
                termsess.SESSIONS[k].kill()
            except Exception:
                pass
        with termsess.SESSIONS_LOCK:
            termsess.SESSIONS.clear()

    return result


# ---------------------------------------------------------------------------
# Hosted-app proxy:  /<folder>/<port>/...  ->  http://127.0.0.1:<port>/...
# ---------------------------------------------------------------------------
def _do_proxy(folder, port, rest=""):
    key = (folder, port)
    if key not in HOSTS:
        abort(404)
    url = f"http://127.0.0.1:{port}/{rest}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection", "cookie")
    }
    try:
        last_err = None
        resp = None
        for _ in range(10):
            try:
                resp = requests.request(
                    request.method,
                    url,
                    params=request.args,
                    data=request.get_data(),
                    headers=headers,
                    cookies=request.cookies,
                    timeout=30,
                    allow_redirects=False,
                )
                break
            except requests.RequestException as e:
                last_err = e
                time.sleep(0.3)
        if resp is None:
            raise last_err
    except requests.RequestException:
        abort(502)
    excluded = ("content-length", "content-encoding", "transfer-encoding",
                "connection", "server")
    out_headers = [
        (k, v) for k, v in resp.headers.items()
        if k.lower() not in excluded
    ]
    return Response(resp.content, status=resp.status_code, headers=out_headers)


app.add_url_rule("/<folder>/<int:port>", "proxy_noslash",
                  lambda folder, port: _do_proxy(folder, port, ""),
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
app.add_url_rule("/<folder>/<int:port>/", "proxy_slash", _do_proxy,
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
app.add_url_rule("/<folder>/<int:port>/<path:rest>", "proxy_rest", _do_proxy,
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])


# ---------------------------------------------------------------------------
# Interactive terminal: stream a real PTY over a websocket.
# ---------------------------------------------------------------------------
@app.route("/api/tabs")
def api_tabs():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    username = session["username"]
    sess = state.get_session(username)
    tabs = []
    if sess:
        for tid, t in sess["tabs"].items():
            s = termsess.get(username, tid)
            tabs.append({
                "id": tid,
                "name": t.get("name", tid),
                "cwd": t.get("cwd"),
                "is_terminal": s is not None,
                "alive": bool(s and s.alive and not s.exited),
                "cmd": t.get("cmd", ""),
            })
    return {"tabs": tabs}


@app.route("/api/close_tab", methods=["POST"])
def api_close_tab():
    if "username" not in session:
        return {"error": "unauthorized"}, 401
    username = session["username"]
    data = request.get_json(silent=True) or {}
    tab_id = data.get("tab")
    if not tab_id:
        return {"ok": False}, 400
    state.remove_tab(username, tab_id)
    with termsess.SESSIONS_LOCK:
        s = termsess.SESSIONS.pop((username, tab_id), None)
    if s:
        s.kill()
    return {"ok": True}


@sock.route("/ws")
def ws_terminal(ws):
    if "username" not in session:
        return
    username = session["username"]
    sess = state.get_session(username)
    if not sess:
        return
    tab_id = request.args.get("tab", "main")
    tab = state.ensure_tab(sess, username, tab_id)
    cmd = request.args.get("cmd") or ""
    if cmd:
        state.set_terminal(sess, username, tab_id, cmd, tab["cwd"])
    cols = int(request.args.get("cols", 80))
    rows = int(request.args.get("rows", 24))
    ptyhost.attach(ws, username, tab_id, cmd, tab["cwd"], cols, rows)


def main():
    auth.seed_defaults()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
