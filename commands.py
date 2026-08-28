"""Command implementations for the rconsole shell.

Most commands (ls, cat, mkdir, rm, cp, mv, python, git, ...) are NOT implemented here;
they are executed on the real host via the subprocess passthrough in interp.py.
The commands below are the "console-native" ones that need special handling:
navigation/state, auth, and hosting.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import auth
import jobs
import state

HERE = Path(__file__).resolve().parent

# Console-native commands that MUST be prefixed with `sudo` (admin only).
PROTECTED = {"rboot", "cpass", "cuser", "muser", "serve", "gunicorn",
             "sprocess", "kprocess", "suser", "auser", "nauser", "ruser",
             "env", "opencode", "agent"}

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
    std = [
        ("help", "this help"),
        ("clear", "clear the screen"),
        ("cd <dir>", "change directory (stateful)"),
        ("history", "command history for this tab"),
        ("rwhoami", "print the current user"),
        ("edit <file>", "built-in code editor (great on phones)"),
        ("fm [path]", "in-console file manager + editor"),
    ]
    sudo = [
        ("sudo serve <port>", "host cwd as a static site"),
        ("sudo gunicorn <spec> -b :p", "host a WSGI app"),
        ("sudo cpass / cuser", "change password / rename user"),
        ("sudo muser / ruser", "create / delete a user"),
        ("sudo suser / auser / nauser", "list / grant / revoke admin"),
        ("sudo rboot", "factory reset (kill hosts, reset users)"),
    ]
    out = ["Console commands (type a name, then Enter):", ""]
    for name, desc in std:
        out.append(f"  {name:<16} {desc}")
    out.append("")
    out.append("Shell features:")
    out.append("  alias name=cmd        define a persistent command alias")
    out.append("  unalias <name>        remove an alias")
    out.append("  theme [name]          list / switch console themes")
    out.append("  <cmd> &               run a command in the background")
    out.append("  jobs                  list background jobs")
    out.append("  jobout %n [lines]     show a background job's output")
    out.append("  jobtail %n            live-tail a job's output (any key to stop)")
    out.append("  fg %n                 bring a job's output to the foreground")
    out.append("  kill %n               kill a background job")
    out.append("")
    out.append("Fun commands:")
    out.append("  cowsay <text>  fortune  figlet <text>  banner <text>")
    out.append("  lolcat <text>   sl  matrix  neofetch  colors  cal  wc  rev")
    out.append("  date  uptime  echo -e <text>  hi")
    out.append("")
    out.append("Admin commands (need 'sudo'):")
    for name, desc in sudo:
        out.append(f"  {name:<28} {desc}")
    out.append("")
    out.append("Agent / tooling:")
    out.append("  sudo env set KEY=val   store an env var (e.g. api key) for your sessions")
    out.append("  sudo env import        copy provider keys from the server environment")
    out.append("  sudo env               list / sudo env unset KEY")
    out.append("  sudo opencode [args]   launch the opencode AI agent TUI")
    out.append("  sudo agent [prompt]    native in-console AI agent (no PTY; uses sudo env keys)")
    out.append("  sudo agent help        show agent slash commands (/clear /model /keys /models ...)")
    out.append("  sudo agent models      list models available from the current provider")
    out.append("  Inside `sudo agent`:")
    out.append("    /help /clear /model <name> /provider /keys /models /compact /system /exit")
    out.append("")
    out.append("Any other command runs on the REAL host shell:")
    out.append("  ls  pwd  mkdir  cat  rm  cp  mv  echo  whoami  id")
    out.append("  python  pip  git  apt  ...    (cd is native & stateful)")
    out.append("")
    out.append("Hosting is served at /<folder>/<port>/ through rconsole's proxy,")
    out.append("so no extra port-forwarding is needed.")
    out.append("")
    out.append("On a phone, prefer 'edit'/'fm' over nano/vim: the soft keyboard")
    out.append("plus xterm can be fragile for full-screen TUIs.")
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
# Aliases (per-user, persisted)
# ---------------------------------------------------------------------------

@command("alias")
def cmd_alias(ctx, args):
    """Define or list command aliases.

    Usage:
      alias                 -> list all aliases
      alias name=command    -> define an alias (quotes optional)
      alias name            -> show one alias
    """
    sid = ctx["sid"]
    if not args:
        a = state.get_aliases(sid)
        if not a:
            return "No aliases defined. Try: alias ll=ls -la"
        out = ["Aliases:"]
        for k in sorted(a):
            out.append(f"  alias {k}='{a[k]}'")
        return "\n".join(out)
    arg = " ".join(args)
    if "=" not in arg:
        name = arg.strip()
        a = state.get_aliases(sid).get(name)
        if a is None:
            return f"alias: {name}: not found"
        return f"alias {name}='{a}'"
    name, value = arg.split("=", 1)
    name = name.strip()
    value = value.strip().strip("'\"")
    if not name or " " in name:
        return "alias: invalid name (no spaces allowed)"
    state.set_alias(sid, name, value)
    return ""


@command("unalias")
def cmd_unalias(ctx, args):
    if not args:
        return "Usage: unalias <name>"
    ok = state.remove_alias(ctx["sid"], args[0].strip())
    if not ok:
        return f"unalias: {args[0]}: not found"
    return ""


# ---------------------------------------------------------------------------
# Themes (per-user, persisted; applied live in the browser)
# ---------------------------------------------------------------------------

THEMES = ["default", "light", "amber", "solarized", "dracula", "github",
          "ubuntu", "retro", "nord", "gruvbox", "matrix", "tokyonight",
          "catppuccin", "monokai", "everforest", "ayu", "papercolor"]


@command("theme")
def cmd_theme(ctx, args):
    """List or switch console themes.

    Usage:
      theme            -> list available themes + current
      theme <name>     -> apply a theme (persisted for your account)
    """
    sid = ctx["sid"]
    sess = state.get_session(sid)
    current = sess.get("theme", "default") if sess else "default"
    if not args:
        out = ["Available themes (use: theme <name>):", ""]
        for t in THEMES:
            mark = " *" if t == current else ""
            out.append(f"  {t:<12}{mark}")
        out.append("")
        out.append(f"Current theme: {current}")
        return "\n".join(out)
    name = args[0].strip().lower()
    if name not in THEMES:
        return f"theme: unknown theme '{name}'. Try: theme  (to list)"
    state.set_theme(sid, name)
    return {"output": f"Theme set to '{name}'.", "theme": name}


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
# Background jobs:  cmd &   -> start detached;  jobs / jobout / kill %n
# ---------------------------------------------------------------------------

@command("jobs")
def cmd_jobs(ctx, args):
    """List background jobs started with '&'."""
    sid = ctx["sid"]
    listing = jobs.list_jobs(sid)
    if not listing:
        return "No background jobs running. Start one with:  sleep 30 &"
    out = ["Job   PID    STATE      COMMAND", "-" * 50]
    for jid, job in listing:
        state_ = "running" if job.alive else "done"
        pid = str(job.pid) if job.pid else "-"
        out.append(f"  {jid:<3}  {pid:<6} {state_:<10} {job.cmd[:40]}")
    out.append("")
    out.append("Inspect output:  jobout %n   |   Live:  jobtail %n / fg %n   |   Kill:  kill %n")
    return "\n".join(out)


@command("jobout")
def cmd_jobout(ctx, args):
    """Show the last lines of a background job's output: jobout %n [lines]."""
    if not args:
        return "Usage: jobout %n [lines]"
    jid = jobs.parse_job_ref(args[0])
    if jid is None:
        return f"jobout: invalid job: {args[0]}"
    job = jobs.get_job(ctx["sid"], jid)
    if not job:
        return f"jobout: no such job: {jid}"
    n = 50
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            return "jobout: lines must be an integer"
    head = f"--- job {jid} ({'running' if job.alive else 'done'}) ---"
    return head + "\n" + job.tail(n)


@command("jobtail")
def cmd_jobtail(ctx, args):
    """Live-tail a background job's output: jobtail %n (press any key to stop)."""
    if not args:
        return "Usage: jobtail %n   (live tail; press any key to stop)"
    jid = jobs.parse_job_ref(args[0])
    if jid is None:
        return f"jobtail: invalid job: {args[0]}"
    job = jobs.get_job(ctx["sid"], jid)
    if not job:
        return f"jobtail: no such job: {jid}"
    return {"jobtail": jid}


@command("fg")
def cmd_fg(ctx, args):
    """Bring a background job's output to the foreground: fg %n.

    Streams the job's output live and stops automatically when the job ends.
    """
    if not args:
        return "Usage: fg %n   (foreground a job; stops when it ends)"
    jid = jobs.parse_job_ref(args[0])
    if jid is None:
        return f"fg: invalid job: {args[0]}"
    job = jobs.get_job(ctx["sid"], jid)
    if not job:
        return f"fg: no such job: {jid}"
    return {"jobtail": jid, "fg": True}


@command("kill")
def cmd_kill(ctx, args):
    """Kill processes.

    Usage:
      kill %n          -> kill background job n (see: jobs)
      kill <pid> ...   -> delegate to the real host kill
      kill <signal> %n -> send a signal name to job n (e.g. kill SIGKILL %1)
    """
    import interp as _interp
    if not args:
        return "Usage: kill %n | kill <pid>..."
    # Job reference '%n' anywhere -> kill that job.
    for a in args:
        if a.startswith("%"):
            jid = jobs.parse_job_ref(a)
            if jid is None:
                return f"kill: invalid job: {a}"
            if jobs.kill_job(ctx["sid"], jid):
                return f"Background job {jid} terminated."
            return f"kill: no such job: {jid}"
    # Otherwise delegate to the real host kill (e.g. kill -9 1234).
    return _interp._passthrough(ctx, "kill " + " ".join(args))


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


# ---------------------------------------------------------------------------
# Environment variables (per-user, persisted). Exported into every PTY / shell
# session via state.get_env() so tools like opencode pick up API keys without
# the server itself needing those vars at startup (no redeploy required).
# ---------------------------------------------------------------------------

_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "GROQ_API_KEY", "AZURE_OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY", "DEEPSEEK_API_KEY", "OPENCODE_API_KEY", "ZEN_API_KEY",
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENCODE_BASE_URL", "ZEN_BASE_URL",
    "OPENCODE_MODEL", "ZEN_MODEL",
)


@command("env")
def cmd_env(ctx, args):
    """Manage environment variables injected into every session.

    Usage:
      sudo env                    list variables (values masked)
      sudo env set KEY=VALUE      add/update a variable (e.g. OPENAI_API_KEY=sk-...)
      sudo env unset KEY          remove a variable
      sudo env import            copy known provider keys already in the server env

    These persist for your account and are exported into every command you run,
    so opencode / other tools see your API keys automatically."""
    sid = ctx["sid"]
    if not args:
        env = state.get_env(sid)
        if not env:
            return ("No custom environment variables set. Try:\n"
                    "  sudo env set OPENAI_API_KEY=sk-...\n"
                    "  sudo env import")
        out = ["Custom environment (exported to your sessions):", ""]
        for k in sorted(env):
            v = env[k]
            masked = (v[:4] + "…" + v[-2:]) if len(v) > 8 else "***"
            out.append(f"  {k}={masked}")
        return "\n".join(out)
    sub = args[0]
    if sub == "import":
        imported = []
        for k in _PROVIDER_KEYS:
            v = os.environ.get(k)
            if v:
                state.set_env(sid, k, v)
                imported.append(k)
        if imported:
            return "Imported from server environment:\n  " + "\n  ".join(imported)
        return "No known provider keys found in the server environment to import."
    if sub == "set":
        if len(args) < 2 or "=" not in args[1]:
            return "Usage: sudo env set KEY=VALUE"
        key, val = args[1].split("=", 1)
        key, val = key.strip(), val.strip().strip('"\'')
        if not key:
            return "env: invalid key"
        state.set_env(sid, key, val)
        return f"Set {key}."
    if sub == "unset":
        if len(args) < 2:
            return "Usage: sudo env unset KEY"
        ok = state.remove_env(sid, args[1].strip())
        return f"Removed {args[1]}." if ok else f"env: {args[1]}: not set"
    return "Usage: sudo env [set KEY=val | unset KEY | import]"


@command("opencode")
def cmd_opencode(ctx, args):
    """Launch the opencode AI agent TUI (sudo opencode [args...]).

    The opencode binary is located automatically: a local install at
    ./node_modules/.bin/opencode is preferred, then one on PATH. If neither is
    found, `opencode-ai` is installed locally in the current directory as a
    background job (no global install permission required). API keys are injected
    from `sudo env`, so opencode picks them up automatically.

    Tip: if the interactive TUI is too heavy for your host, use the non-interactive
    mode instead:  sudo opencode run "your prompt"  (or `opencode run ...`)."""
    from pathlib import Path as _P
    cwd = ctx["tab"]["cwd"]
    local_bin = _P(cwd) / "node_modules" / ".bin" / "opencode"
    if local_bin.exists():
        bin_cmd = str(local_bin)
    else:
        import shutil as _shutil
        bin_cmd = _shutil.which("opencode")
        if not bin_cmd:
            # No global install permission on many hosts, so install locally.
            jid, _ = jobs.start_job(ctx["sid"], "npm install opencode-ai", cwd)
            return {"output":
                f"opencode not found — started `npm install opencode-ai` as job {jid}.\n"
                f"When it finishes (check `jobs`), run `sudo opencode` again."}
    extra = " ".join(args)
    cmd = (bin_cmd + " " + extra).strip()
    return {"pty": True, "cmd": cmd}



@command("rboot")
def cmd_rboot(ctx, args):
    # Hard reset: kill hosted apps + terminal sessions, wipe every user's
    # persisted state, reset in-memory state, and reset users to defaults.
    import os as _os
    from pathlib import Path as _P

    username = ctx["username"]
    state.kill_all_hosts(username)
    try:
        jobs.kill_all_jobs(username)
    except Exception:
        pass
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


# ---------------------------------------------------------------------------
# In-console file manager + editor (pure Python, no PTY / soft-keyboard).
# Fully interactive through the normal line console, so they are smooth and
# reliable on mobile phones where xterm + a soft keyboard is fragile.
# ---------------------------------------------------------------------------

def _resolve_path(ctx, target):
    target = (target or "").strip()
    p = Path(target)
    if not p.is_absolute():
        p = Path(ctx["tab"]["cwd"]) / target
    return p.resolve()


def _raw_arg(ctx):
    """Return the argument portion of the raw input line, preserving characters
    (e.g. Windows backslashes) that the shlex tokenizer would otherwise mangle."""
    raw = ctx.get("raw", "") or ""
    parts = raw.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    except IsADirectoryError:
        return None
    except Exception:
        return None
    if data == "":
        return []
    if data.endswith("\n"):
        data = data[:-1]
    return data.split("\n")


def _write_lines(path, lines):
    content = "\n".join(lines)
    if lines:
        content += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _render_editor(p, lines, dirty, cursor, mode, status):
    out = []
    tag = "  [modified]" if dirty else ""
    out.append(f"Editing: {p}   ({len(lines)} lines{tag})")
    out.append("-" * 60)
    if not lines:
        out.append("  (empty file)")
    for i, ln in enumerate(lines):
        mark = "> " if i == cursor else "  "
        out.append(f"{mark}{i + 1} | {ln}")
    if cursor == -1:
        out.append(">  (cursor before first line)")
    out.append("-" * 60)
    if mode == "edit":
        cur = lines[cursor] if 0 <= cursor < len(lines) else ""
        out.append(f"Editing line {cursor + 1}. Current: {cur}")
        out.append("Type the new text ('.' on its own cancels):")
    elif mode == "insert":
        out.append("Insert a new line before the cursor. Type text ('.' cancels):")
    else:
        out.append("Type text + Enter -> insert a line AFTER the cursor (keep going to write line by line).")
        out.append("up/down or j/k -> move   e -> edit line   i -> insert before   d -> delete")
        out.append("w -> save   r -> reload   q -> quit   ? -> help")
    if status:
        out.append("> " + status)
    return "\n".join(out)


EDITOR_HELP = (
    "Nano-style editor:\n"
    "  (type text) + Enter   insert a new line AFTER the cursor\n"
    "  up / down  (or j / k) move the cursor between lines\n"
    "  e                  edit the current line (then type its replacement)\n"
    "  i                  insert a new line BEFORE the cursor\n"
    "  a                  append a blank line at the end (then edit it)\n"
    "  d                  delete the current line\n"
    "  w                  save to disk\n"
    "  wq                 save and quit\n"
    "  r                  reload from disk (discard unsaved changes)\n"
    "  q                  quit (warns if unsaved)\n"
    "  q!                 quit discarding changes\n"
    "  ?                  show this help\n"
    "Tip: open a file, then just type lines and press Enter to write it top-to-bottom."
)


def edit_routine(path, ctx):
    p = Path(path)
    if p.is_dir():
        return {"output": f"edit: {path}: Is a directory"}
    existed = p.exists()
    try:
        lines = _read_lines(p)
    except Exception as e:
        return {"output": f"edit: cannot read {p}: {e}"}
    if lines is None:
        return {"output": f"edit: {path}: cannot open (is it a directory?)"}
    dirty = False
    cursor = -1 if not lines else 0
    mode = "normal"
    status = "New file - will be created on first save." if not existed else f"Loaded {len(lines)} lines."
    while True:
        prompt = "edit> "
        if mode == "edit":
            prompt = f"L{cursor + 1}> "
        elif mode == "insert":
            prompt = "ins> "
        view = _render_editor(p, lines, dirty, cursor, mode, status)
        cmd = yield {"prompt": prompt, "clear": True, "output": view}
        raw = cmd if cmd is not None else ""

        # Two-step "edit this line" / "insert before" sub-prompts capture the
        # raw next line of text (so it can start with any character, even a
        # command name) - a lone '.' cancels.
        if mode == "edit":
            if raw.strip() == ".":
                status = "Edit cancelled."
            else:
                lines[cursor] = raw
                dirty = True
                status = f"Line {cursor + 1} updated."
            mode = "normal"
            continue
        if mode == "insert":
            if raw.strip() == ".":
                status = "Insert cancelled."
            else:
                lines.insert(cursor, raw)
                cursor += 1
                dirty = True
                status = f"Inserted line {cursor + 1}."
            mode = "normal"
            continue

        # Normal mode.
        c = raw.strip()
        if c == "":
            continue
        if c in ("?", "h", "help"):
            status = EDITOR_HELP
            continue
        if c in ("q", "quit"):
            if dirty:
                status = "Unsaved changes! 'w' to save, 'q!' to discard, 'r' to reload."
                continue
            break
        if c == "q!":
            break
        if c in ("w", "wq", "w!"):
            try:
                _write_lines(p, lines)
                dirty = False
                status = f"Saved {len(lines)} lines to {p}."
            except Exception as e:
                status = f"Save failed: {e}"
                if c == "wq":
                    continue
                else:
                    continue
            if c == "wq":
                break
            continue
        if c == "r":
            try:
                reloaded = _read_lines(p)
                lines = reloaded if reloaded is not None else []
                dirty = False
                cursor = -1 if not lines else 0
                status = "Reloaded from disk."
            except Exception as e:
                status = f"Reload failed: {e}"
            continue
        if c in ("up", "k"):
            if cursor > -1:
                cursor -= 1
            status = ""
            continue
        if c in ("down", "j"):
            if cursor < len(lines) - 1:
                cursor += 1
            status = ""
            continue
        if c in ("d", "dd"):
            if 0 <= cursor < len(lines):
                lines.pop(cursor)
                dirty = True
                if cursor >= len(lines):
                    cursor = len(lines) - 1
                status = "Deleted current line."
            else:
                status = "No line at the cursor."
            continue
        if c == "e":
            if 0 <= cursor < len(lines):
                mode = "edit"
                status = ""
            else:
                status = "No line at the cursor to edit."
            continue
        if c == "i":
            mode = "insert"
            status = ""
            continue
        if c == "a":
            lines.append("")
            cursor = len(lines) - 1
            mode = "edit"
            status = ""
            continue
        # Default: insert a new line after the cursor (the "write line by line"
        # flow - the cursor follows each new line so Enter keeps appending).
        insert_at = cursor + 1
        lines.insert(insert_at, raw)
        cursor = insert_at
        dirty = True
        status = f"Inserted line {cursor + 1}."
    return {"output": f"Editor closed: {p}."}


@command("edit")
def cmd_edit(ctx, args):
    """Open a file in the built-in code editor (syntax highlighting, smooth on mobile).

    Usage:  edit <file>
    Launches the in-browser code editor (highlighting + save). Great on phones.
    """
    if not args:
        return "Usage: edit <file>   (opens the code editor)"
    target = _raw_arg(ctx) or args[0]
    path = _resolve_path(ctx, target)
    if path.is_dir():
        return f"edit: {path}: Is a directory"
    return {"editor": str(path)}


# --- file manager -----------------------------------------------------------

DIR_TAG = "[D]"


def _fm_listing(cwd):
    out = []
    out.append(f"File manager: {cwd}")
    out.append("-" * 60)
    try:
        entries = sorted(
            os.listdir(cwd),
            key=lambda s: (not os.path.isdir(os.path.join(cwd, s)), s.lower()),
        )
    except Exception as e:
        out.append(f"Cannot list: {e}")
        return "\n".join(out)
    if not entries:
        out.append("(empty)")
    for name in entries:
        full = os.path.join(cwd, name)
        if os.path.isdir(full):
            out.append(f"{DIR_TAG} {name}/")
        else:
            try:
                sz = os.path.getsize(full)
            except Exception:
                sz = 0
            out.append(f"    {name}  ({sz} bytes)")
    out.append("-" * 60)
    out.append("Cmds: cd <d>  up  open <f>  cat <f>  rm <f>  mkdir <d>  "
               "touch <f>  mv <a> <b>  q=quit  ?=help")
    return "\n".join(out)


FM_HELP = (
    "File manager commands:\n"
    "  cd <dir>    change into a directory (relative or absolute)\n"
    "  up          go up one directory\n"
    "  open <file> edit a file (in-console editor)\n"
    "  cat <file>  print a file's contents\n"
    "  rm <file>   delete a file or empty directory\n"
    "  mkdir <dir> create a directory\n"
    "  touch <f>   create an empty file\n"
    "  mv <a> <b>  rename / move\n"
    "  q           quit (cd persists to your console)\n"
    "  ?           show this help"
)


def fm_routine(start, ctx):
    cwd = _resolve_path(ctx, start)
    if not cwd.is_dir():
        cwd = Path(ctx["tab"]["cwd"])
    status = ""
    pending = None
    while True:
        listing = _fm_listing(cwd)
        if pending is not None:
            listing += "\n" + pending
            pending = None
        if status:
            listing += "\n> " + status
        cmd = yield {"prompt": "fm> ", "clear": True, "output": listing}
        cmd = (cmd or "").strip()
        if cmd == "":
            status = ""
            continue
        if cmd in ("?", "h", "help"):
            status = FM_HELP
            continue
        if cmd in ("q", "quit", "exit"):
            ctx["tab"]["cwd"] = str(cwd)
            return {"output": f"File manager closed. cwd is now {cwd}."}
        if cmd in ("up", ".."):
            cwd = cwd.parent
            status = ""
            continue
        if cmd == "ls":
            status = ""
            continue
        if cmd == "pwd":
            status = f"cwd: {cwd}"
            continue
        if cmd.startswith("cd "):
            target = cmd[3:].strip()
            nc = _resolve_path(ctx, target)
            if nc.is_dir():
                cwd = nc
                ctx["tab"]["cwd"] = str(cwd)
                status = ""
            else:
                status = f"Not a directory: {target}"
            continue
        if cmd.startswith("open ") or cmd.startswith("edit "):
            name = cmd.split(" ", 1)[1].strip()
            path = _resolve_path(ctx, name)
            if path.is_dir():
                status = f"Not a file: {name}"
                continue
            # Hand off to the in-browser code editor (returns, ending the fm
            # session; closing the editor drops back to the console).
            return {"editor": str(path)}
        if cmd.startswith("cat "):
            name = cmd[4:].strip()
            path = _resolve_path(ctx, name)
            try:
                data = _read_lines(path)
                if data is None:
                    status = f"Cannot cat: {name} (directory?)"
                else:
                    pending = f"--- {name} ---\n" + "\n".join(data)
                    status = ""
            except Exception as e:
                status = f"cat failed: {e}"
            continue
        if cmd.startswith("mkdir "):
            name = cmd[6:].strip()
            try:
                os.makedirs(_resolve_path(ctx, name), exist_ok=True)
                status = f"Created {name}"
            except Exception as e:
                status = f"mkdir failed: {e}"
            continue
        if cmd.startswith("touch "):
            name = cmd[6:].strip()
            try:
                open(_resolve_path(ctx, name), "a").close()
                status = f"Touched {name}"
            except Exception as e:
                status = f"touch failed: {e}"
            continue
        if cmd.startswith("rm "):
            name = cmd[3:].strip()
            path = _resolve_path(ctx, name)
            try:
                if path.is_dir():
                    os.rmdir(path)
                else:
                    os.remove(path)
                status = f"Removed {name}"
            except Exception as e:
                status = f"rm failed: {e}"
            continue
        if cmd.startswith("mv "):
            parts = cmd[3:].split(None, 1)
            if len(parts) == 2:
                a = _resolve_path(ctx, parts[0])
                b = _resolve_path(ctx, parts[1])
                try:
                    os.rename(a, b)
                    status = f"Moved {parts[0]} -> {parts[1]}"
                except Exception as e:
                    status = f"mv failed: {e}"
            else:
                status = "Usage: mv <src> <dst>"
            continue
        status = f"Unknown command: {cmd}  (type ? for help)"


@command("fm")
def cmd_fm(ctx, args):
    """Open the in-console file manager (navigate + edit files, mobile-friendly).

    Usage:  fm [path]
    Browse directories and open files in the Python editor without a PTY.
    """
    start = _raw_arg(ctx) or (args[0] if args else ctx["tab"]["cwd"])
    return fm_routine(start, ctx)


# Register the fun/novelty commands (cowsay, fortune, figlet, ...).
import fun  # noqa: E402,F401

# Register the native in-console AI agent (a PTY-free alternative to opencode).
import agent  # noqa: E402,F401
COMMANDS["agent"] = agent.cmd_agent
