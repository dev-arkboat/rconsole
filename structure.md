# rconsole — file structure

```
rconsole/
├── app.py                  # Flask app: routes, auth guard, hosted-app proxy, /ws endpoint
├── main.py                 # Entry point (calls app.main())
├── interp.py               # Command parsing/dispatch; interactive detection; host passthrough
├── commands.py             # Native console commands (help, cd, hosting, users, sprocess, kprocess…)
├── ptyhost.py              # WebSocket ⇄ PTY streaming (winpty/ConPTY + posix)
├── state.py                # In-memory session/tab/host state + process registry
├── auth.py                 # User auth & persistence (data/users.json)
├── _static_host.py         # Bootstrap: serves a directory over HTTP (used by `serve`)
├── _wsgi_host.py           # Bootstrap: wsgiref fallback WSGI server (used by `gunicorn` when real gunicorn is unavailable)
├── pyproject.toml          # Project metadata + dependencies (uv)
├── uv.lock                 # Resolved dependency lockfile (uv)
├── .python-version         # Pinned Python version
├── .gitignore              # Ignored files (venv, caches, data, logs)
├── README.md               # Project documentation
├── structure.md            # This file
├── static/                 # Front-end assets (served as static)
│   ├── console.js          # Tab/terminal UI, xterm.js wiring, WebSocket client
│   ├── style.css           # Console styling + terminal layout
│   ├── xterm.js            # xterm.js terminal emulator
│   ├── xterm.css           # xterm.js stylesheet
│   └── addon-fit.js        # xterm.js FitAddon (auto-fit terminal to container)
├── templates/              # Jinja2 templates
│   ├── console.html        # Main console page (loads xterm.js + console.js)
│   └── login.html           # Login page
├── data/                   # Runtime data (gitignored)
│   └── users.json         # User accounts (hashed passwords)
└── .venv/                  # Virtual environment (gitignored)
```

---

## Module descriptions

### Backend (Python)

**`app.py`** — The Flask application.
- `guard()` (before-request): enforces login, 6h inactivity timeout, and that
  the logged-in user still exists.
- `login` / `console_page`: auth + console rendering.
- `/api/run`: dispatches a line of input to `interp.process`.
- `/<folder>/<int:port>/…`: reverse-proxy (`_do_proxy`) forwarding to the
  registered hosted app at `127.0.0.1:<port>`.
- `/ws`: WebSocket route → `ptyhost.run_pty`, streaming a real PTY.
- `main()`: starts the dev server on `PORT` (default 5000).

**`main.py`** — Thin entry point that imports `app.main` and runs it.

**`interp.py`** — Command interpreter.
- `process()`: routes built-ins to `commands.py`, interactive commands to a PTY
  (`{"pty": True, "cmd": …}`), and everything else to a real host-shell
  passthrough (`_passthrough`, 60s timeout).
- `INTERACTIVE`: the set of commands run through a real PTY.
- `_detect_shell()`: picks `sh`/`bash` (or Git Bash) on Windows, `cmd.exe` on
  native Windows, and `sh`/`bash` on Linux/macOS.

**`commands.py`** — Console-native commands.
- Public: `help`, `clear`/`cls`, `history`, `rwhoami`, `cd`.
- Admin-only (`sudo`): `cuser`, `cpass`, `muser`, `logout`, `rboot`,
  `serve`, `gunicorn`, `sprocess`, `kprocess`.
- `serve` / `gunicorn` launch real server processes and register them in
  `state.ACTIVE_HOSTS` (path: `_static_host.py` / `_wsgi_host.py`).

**`ptyhost.py`** — WebSocket ⇄ PTY bridge.
- `run_pty()`: picks backend by `sys.platform`.
- `_run_pty_win()`: Windows ConPTY via `pywinpty` (lazy import). Reader streams
  PTY output to the socket; watchdog detects child exit; receive loop is
  stop-aware and ends the session on exit. `set_size(cols, rows)` applies
  resize.
- `_run_pty_posix()`: Linux/macOS using `pty`, `select`, `termios`. Same
  lifecycle; teardown uses `proc.terminate()` + `os.killpg(SIGKILL)`.

**`state.py`** — Global registry.
- `STATE`: per-session tabs/state.
- `ACTIVE_HOSTS`: live hosted apps keyed by `(folder, port)` with their
  `Popen` handles.
- `register_host` / `kill_host` / `kill_all_hosts`: lifecycle management.

**`auth.py`** — Authentication.
- Hashes with `sha256(password + PEPPER)`.
- `seed_defaults()` creates `ark`/`ark` admin on first run.
- `verify`, `get_user`, `list_users`, `create_user`, `set_password`,
  `set_username`, `reset_to_defaults`.

**`_static_host.py`** — Hosts a directory over HTTP
(`SimpleHTTPRequestHandler`) on `127.0.0.1:<port>`. Used by `serve`.

**`_wsgi_host.py`** — wsgiref fallback server for WSGI apps. `sudo gunicorn`
prefers the real `gunicorn` module (`python -m gunicorn <spec> -b
127.0.0.1:<port> --chdir <cwd>`); when gunicorn cannot run on the current
platform, this wsgiref server is used instead.

### Front-end (static/ + templates/)

**`templates/console.html`** — Loads `xterm.js`, `addon-fit.js`, and
`console.js`; defines the tab bar, terminal container (`#terms`), and footer
key bar (Tab / Esc / Ctrl / arrows) for touch devices.

**`static/console.js`** — UI controller.
- Tab management; line-mode input; `/api/run` calls.
- `openPty()` / `closePty()` / `updateView()`: xterm.js lifecycle, FitAddon
  auto-fit, WebSocket send/receive, sticky Ctrl for control chars.

**`static/style.css`** — Layout & theming, including the terminal host sizing
that makes the PTY fill the available width.

**`static/xterm.js`, `static/xterm.css`, `static/addon-fit.js`** — Vendored
xterm.js emulator and FitAddon.

### Data & environment

**`data/`** — Runtime state. Contains `users.json` (hashed credentials).
Gitignored; do not commit.

**`pyproject.toml` / `uv.lock`** — Dependencies: `flask`, `flask-sock`,
`requests`, and `pywinpty` (Windows only). Managed with `uv`.

**`.python-version`** — Pinned Python (>= 3.14).
