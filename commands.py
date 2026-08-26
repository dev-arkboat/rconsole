"""Command implementations for the rconsole shell.

Most commands (ls, cat, mkdir, rm, python, git, ...) are NOT implemented here;
they are executed on the real host via the subprocess passthrough in interp.py.
The commands below are the "console-native" ones that need special handling:
navigation/state, auth, and hosting.
"""
import subprocess
import sys
from pathlib import Path

import auth
import state

HERE = Path(__file__).resolve().parent

# Console-native commands that MUST be prefixed with `sudo` (admin only).
PROTECTED = {"rboot", "cpass", "cuser", "muser", "serve", "gunicorn",
             "sprocess", "kprocess", "suser", "auser", "nauser", "ruser"}

# Registry of built-in commands.
COMMANDS = {}


def command(name):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco


def _folder_name(path):
    return Path(path).name or "root"


def _parse_port(args):
    """Extract a port from args, supporting --bind host:port / -b :port."""
    port = 8000
    for i, a in enumerate(args):
        if a in ("--bind", "-b") and i + 1 < len(args):
            val = args[i + 1]
            if ":" in val:
                try:
                    port = int(val.rsplit(":", 1)[1])
                except ValueError:
                    pass
        elif a.startswith("--bind="):
            val = a.split("=", 1)[1]
            if ":" in val:
                try:
                    port = int(val.rsplit(":", 1)[1])
                except ValueError:
                    pass
    for a in reversed(args):
        if a.isdigit():
            port = int(a)
            break
    return port


# ---------------------------------------------------------------------------
# Console-native built-ins
# ---------------------------------------------------------------------------

@command("help")
def cmd_help(ctx, args):
    std = ["help", "clear", "cd", "history", "rwhoami"]
    sudo_cmds = sorted(PROTECTED)
    out = ["Console-native commands (stateful / auth / hosting):", ""]
    out.append("  " + "  ".join(std))
    out.append("")
    out.append("Commands requiring 'sudo' (admin only):")
    out.append("  " + "  ".join(sudo_cmds))
    out.append("")
    out.append("Everything else runs on the REAL host shell,")
    out.append("e.g.  ls  pwd  mkdir  cat  rm  cp  mv  echo  whoami  id")
    out.append("      python  pip  git  apt  ...   (cd is native & stateful)")
    out.append("")
    out.append("Hosting (sudo):")
    out.append("  sudo serve <port>                      -> serve cwd at /<folder>/<port>/")
    out.append("  sudo gunicorn app:app -b :<port>       -> same, for WSGI apps")
    return "\n".join(out)


@command("clear")
@command("cls")
def cmd_clear(ctx, args):
    return {"clear": True}


@command("history")
def cmd_history(ctx, args):
    hist = ctx["tab"]["history"]
    return "\n".join(f"{i+1}  {c}" for i, c in enumerate(hist))


@command("rwhoami")
def cmd_rwhoami(ctx, args):
    return ctx["username"]


@command("cd")
def cmd_cd(ctx, args):
    target = args[0] if args else state.default_cwd()
    cwd = Path(ctx["tab"]["cwd"])
    p = Path(target) if Path(target).is_absolute() else cwd / target
    p = p.resolve()
    if p.is_dir():
        ctx["tab"]["cwd"] = str(p)
        return ""
    if p.exists():
        return f"cd: {args[0]}: Not a directory"
    return f"cd: {args[0]}: No such file or directory"


# ---------------------------------------------------------------------------
# Hosting (serve / gunicorn) — uses the REAL current directory
# ---------------------------------------------------------------------------

def _use_gunicorn():
    """Return True only if the real gunicorn module can actually run here.

    gunicorn installs on platforms where it cannot start (e.g. native
    Windows), so we probe with `--version` rather than just importing it.
    When unavailable, callers fall back to the lightweight wsgiref server.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "gunicorn", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _launch(ctx, port, wsgi_spec=None):
    if not (1024 <= port <= 65535):
        return f"Port must be between 1024 and 65535 (got {port})"
    real_cwd = ctx["tab"]["cwd"]
    if not Path(real_cwd).is_dir():
        return f"serve: {real_cwd}: not a directory"
    folder = _folder_name(real_cwd)
    key = (folder, port)
    if key in state.ACTIVE_HOSTS:
        old = state.ACTIVE_HOSTS[key]
        state.kill_host(old["sid"], folder, port)

    if wsgi_spec:
        if _use_gunicorn():
            # Prefer the real gunicorn WSGI server.
            proc = subprocess.Popen(
                [sys.executable, "-m", "gunicorn", wsgi_spec,
                 "-b", f"127.0.0.1:{port}", "--chdir", real_cwd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            backend = "gunicorn"
        else:
            # Fallback: lightweight wsgiref server.
            proc = subprocess.Popen(
                [sys.executable, HERE / "_wsgi_host.py",
                 wsgi_spec, str(port), real_cwd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            backend = "wsgiref"
    else:
        proc = subprocess.Popen(
            [sys.executable, HERE / "_static_host.py", str(port), real_cwd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        backend = "http.server"

    state.register_host(ctx["sid"], folder, port, proc)
    return (f"Hosting '{real_cwd}' at /{folder}/{port}/\n"
            f"Open: /{folder}/{port}/  (process pid {proc.pid}, backend: {backend})")


@command("serve")
def cmd_serve(ctx, args):
    if not args or not args[0].isdigit():
        return "Usage: sudo serve <port>"
    return _launch(ctx, int(args[0]))


@command("gunicorn")
def cmd_gunicorn(ctx, args):
    if not args:
        return "Usage: sudo gunicorn <module:app> -b :<port>"
    spec = args[0]
    if ":" not in spec:
        return "gunicorn: expected <module:app> (e.g. app:app)"
    port = _parse_port(args[1:])
    return _launch(ctx, port, wsgi_spec=spec)


@command("sprocess")
def cmd_sprocess(ctx, args):
    """List the running hosted processes (serve/gunicorn) with their PIDs."""
    import state as _state
    sid = ctx["sid"]
    sess = _state.get_session(sid)
    hosts = sess.get("hosts", {}) if sess else {}
    if not hosts:
        return "No hosted processes are running (use: sudo serve <port> / sudo gunicorn <spec> -b :<port>)."
    out = ["Running hosted processes (serve / gunicorn):", ""]
    out.append(f"  {'PORT':<7}{'FOLDER':<22}{'PID':<8}STATE")
    for (folder, port), proc in sorted(hosts.items(), key=lambda kv: kv[0][1]):
        alive = proc is not None and proc.poll() is None
        pid = str(proc.pid) if alive else "-"
        out.append(f"  {port:<7}{folder[:21]:<22}{pid:<8}{'running' if alive else 'exited'}")
    return "\n".join(out)


@command("kprocess")
def cmd_kprocess(ctx, args):
    """Kill a hosted process by PID, or all hosted processes if no PID given.

    Usage:
      sudo kprocess            -> kill every running serve/gunicorn process
      sudo kprocess <pid>      -> kill the process with that PID (hosted or not)
    """
    import os
    import signal
    import state as _state
    sid = ctx["sid"]

    if args:
        try:
            pid = int(args[0])
        except ValueError:
            return f"kprocess: invalid pid: {args[0]!r}"

        # Prefer a tracked hosted process (clean teardown + proxy deregister).
        sess = _state.get_session(sid)
        target = None
        if sess:
            for (folder, port), proc in sess["hosts"].items():
                if proc is not None and proc.pid == pid:
                    target = (folder, port)
                    break
        if target:
            _state.kill_host(sid, target[0], target[1])
            return f"Killed hosted process {pid} (port {target[1]})."
        # Not a tracked host: attempt a direct OS kill as a fallback.
        try:
            os.kill(pid, signal.SIGTERM)
            return f"Sent SIGTERM to process {pid}."
        except ProcessLookupError:
            return f"kprocess: no process with pid {pid}."
        except Exception as e:
            return f"kprocess: could not kill pid {pid}: {e}"

    # No PID -> kill everything this session is hosting.
    sess = _state.get_session(sid)
    count = len(sess.get("hosts", {})) if sess else 0
    _state.kill_all_hosts(sid)
    return f"Killed all hosted processes ({count} stopped)."


# ---------------------------------------------------------------------------
# Protected / admin commands
# ---------------------------------------------------------------------------

@command("logout")
def cmd_logout(ctx, args):
    # Public: anyone (admin or not) may log themselves out.
    return {"action": "logout", "output": "Logging out. Goodbye."}


@command("suser")
def cmd_suser(ctx, args):
    """List all users and their role (admin/user)."""
    import auth as _auth
    users = _auth.list_users()
    if not users:
        return "No users."
    out = ["Users:", ""]
    for name in sorted(users):
        u = _auth.get_user(name) or {}
        role = "admin" if u.get("is_admin") else "user"
        me = " (you)" if name == ctx["username"] else ""
        out.append(f"  {name:<20}{role}{me}")
    return "\n".join(out)


@command("auser")
def cmd_auser(ctx, args):
    """Grant admin rights to a user: sudo auser <username>."""
    if not args:
        return "Usage: sudo auser <username>"
    import auth as _auth
    ok, err = _auth.set_admin(args[0], True)
    if not ok:
        return f"auser: {err}"
    return f"User '{args[0]}' is now an admin."


@command("nauser")
def cmd_nauser(ctx, args):
    """Revoke admin rights from a user: sudo nauser <username>."""
    if not args:
        return "Usage: sudo nauser <username>"
    import auth as _auth
    ok, err = _auth.set_admin(args[0], False)
    if not ok:
        return f"nauser: {err}"
    return f"Admin rights removed from '{args[0]}'."


@command("ruser")
def cmd_ruser(ctx, args):
    """Delete a user: sudo ruser <username>."""
    if not args:
        return "Usage: sudo ruser <username>"
    import auth as _auth
    ok, err = _auth.delete_user(args[0])
    if not ok:
        return f"ruser: {err}"
    return f"User '{args[0]}' deleted."


@command("rboot")
def cmd_rboot(ctx, args):
    # Hard reset: kill hosted apps + terminal sessions, wipe every user's
    # persisted state, reset in-memory state, and reset users to defaults.
    import os as _os
    from pathlib import Path as _P

    username = ctx["username"]
    state.kill_all_hosts(username)
    try:
        import termsess as _ts
        with _ts.SESSIONS_LOCK:
            for k in list(_ts.SESSIONS.keys()):
                try:
                    _ts.SESSIONS[k].kill()
                except Exception:
                    pass
            _ts.SESSIONS.clear()
    except Exception:
        pass

    # Remove all persisted per-user state files so nothing respawns later.
    try:
        d = _P(__file__).resolve().parent / "data" / "state"
        if d.exists():
            for f in d.glob("*.json"):
                try:
                    _os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass

    # Wipe in-memory state for every user.
    try:
        state.STATE.clear()
    except Exception:
        pass

    auth.reset_to_defaults()
    return {"output": "*** HARD REBOOT ***\nSystem reinitialized to factory state.",
            "clear": True,
            "update_session": {"username": "ark"}}


@command("muser")
def cmd_muser(ctx, args):
    def routine():
        username = (yield "New username: ").strip()
        if not username:
            return "muser: empty username"
        password = yield "New password: "
        confirm = yield "Confirm password: "
        if password != confirm:
            return "muser: passwords do not match"
        ok, err = auth.create_user(username, password)
        if not ok:
            return f"muser: {err}"
        return f"User '{username}' created."
    return routine()


@command("cpass")
def cmd_cpass(ctx, args):
    def routine():
        target = (yield f"Change password for [{ctx['username']}]: ").strip()
        if not target:
            target = ctx["username"]
        new = yield "New password: "
        confirm = yield "Confirm password: "
        if new != confirm:
            return "cpass: passwords do not match"
        ok, err = auth.set_password(target, new)
        if not ok:
            return f"cpass: {err}"
        return f"Password updated for '{target}'."
    return routine()


@command("cuser")
def cmd_cuser(ctx, args):
    def routine():
        new = (yield "New username: ").strip()
        if not new:
            return "cuser: empty username"
        ok, err = auth.set_username(ctx["username"], new)
        if not ok:
            return f"cuser: {err}"
        return {"output": f"Username changed to '{new}'.",
                "update_session": {"username": new}}
    return routine()
