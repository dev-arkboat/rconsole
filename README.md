# rconsole — simulated Linux console with hosted web apps

rconsole is a Flask web application that exposes a **simulated Linux console**
in the browser. It blends two modes:

1. **Line-mode console** — a stateful, simulated shell (`cd`, `help`, hosted-app
   management, user administration) implemented natively in Python.
2. **Real interactive terminal** — full-screen, real programs (`nano`, `vim`,
   `python`, `bash`, `less`, `top`, `git`, `ssh`, ...) streamed over a
   WebSocket-backed PTY. This is a *genuine* host shell, not an emulation.

On top of that, rconsole can **host real web apps**: run `sudo serve 8000` (or
`sudo gunicorn …`) and the app is served at `https://your-domain/<folder>/<port>/`
through rconsole's built-in reverse proxy — no extra port-forwarding required.

---

## Features

- Browser terminal powered by **xterm.js** + a real PTY (ConPTY on Windows,
  `pty`/`select` on Linux/macOS).
- Tabs, each owning its own terminal session that keeps running in the
  background and reappears when you switch back.
- Native, stateful console commands plus a real host-shell passthrough.
- Hosted web apps (static sites and WSGI apps) reachable through the same
  domain via a path-based proxy.
- User accounts with admin/non-admin roles, session timeout, and a
  factory-reset command.

---

## Requirements

- Python **>= 3.14** (see `.python-version`).
- [`uv`](https://docs.astral.sh/uv/) for dependency/ environment management.
- On **Windows**: `pywinpty` (ConPTY) is installed automatically and used.
- On **Linux/macOS**: the standard library `pty` module is used; no extra
  native dependency.

> `pywinpty` is declared as `pywinpty>=3.0.5; sys_platform == 'win32'` in
> `pyproject.toml`, so it is never installed or imported off Windows.

---

## Installation & running

```bash
# 1. Create / sync the environment (also installs dependencies)
uv sync

# 2. Run the server
uv run python main.py
#    (equivalently: uv run python app.py)

# 3. Open the console
#    http://127.0.0.1:5000   (default port 5000; override with PORT)
```

First launch seeds a default admin account (see **Default credentials**).

### Configuration (environment variables)

| Variable            | Purpose                                                        | Default                       |
|---------------------|----------------------------------------------------------------|-------------------------------|
| `PORT`              | TCP port the Flask app listens on                              | `5000`                        |
| `RCONSOLE_SECRET`  | Flask `SECRET_KEY` (session signing). **Set in production.**  | random 32-byte hex            |
| `RCONSOLE_PEPPER`  | Static pepper mixed into password hashes                      | `rconsole-static-pepper-change-me` |

Example:

```bash
export PORT=8000
export RCONSOLE_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')"
export RCONSOLE_PEPPER="some-long-random-string"
uv run python main.py
```

---

## Default credentials

On first start `auth.seed_defaults()` creates:

```
username: ark
password: ark
role:     admin
```

Change the password immediately with `sudo cpass`, or create new users with
`sudo muser`.

---

## Using the console

### Line-mode commands (native / simulated)

These run in the simulated shell and are stateful:

| Command            | Description                                                        |
|--------------------|--------------------------------------------------------------------|
| `help`             | List console-native and sudo-protected commands.                   |
| `clear` / `cls`    | Clear the screen.                                                  |
| `cd <dir>`         | Change the simulated working directory (stateful).                 |
| `history`          | Show command history for the tab.                                  |
| `rwhoami`          | Print the current username.                                        |
| `sudo cuser`       | Rename the current user.                                           |
| `sudo cpass`       | Change a password.                                                 |
| `sudo muser`       | Create a new user (interactive prompts).                           |
| `logout`           | Log out (**anyone** may run this, admin or not).                   |
| `sudo suser`       | List all users and their role (admin/user).                        |
| `sudo auser <user>`| Grant admin rights to a user.                                      |
| `sudo nauser <user>`| Revoke admin rights from a user.                                   |
| `sudo ruser <user>`| Delete a user.                                                     |
| `sudo rboot`       | Factory reset: kill hosted apps, reset tabs, reset users to `ark`. |
| `sudo serve <port>`| Host the **current directory** as a static site.                   |
| `sudo gunicorn <module:app> -b :<port>` | Host a WSGI app via the real **gunicorn** server (falls back to wsgiref if gunicorn is unavailable). |
| `sudo sprocess`    | List running hosted processes (serve/gunicorn) with their PIDs.    |
| `sudo kprocess [pid]` | Kill a hosted process by PID, or **all** if no PID is given.     |

Commands prefixed with `sudo` require an **admin** account. The `logout`
command is the exception — any user (admin or not) may run it to end their
session.

### Interactive / real commands (real PTY)

Anything not natively handled is run on the **real host shell**. A curated
set is detected as "interactive" and launched inside a real PTY so
full-screen TUIs and REPLs work:

```
nano  vim  vi  emacs  nvim
less  more  man  most
top  htop  btm  glances  watch
python  python3  py  ipython  node  irb  lua  php
bash  sh  zsh  fish  powershell  pwsh  cmd
ssh  telnet  tmux  screen  lynx  w3m  mutt  irssi
ranger  nnn  mc  fzf  git (+ log/diff/show/status/blame/branch)
```

Example:

```
ark@rconsole:/home/user$ nano app.py
```

The editor opens in the terminal. When you quit (`^X`), the PTY session ends
and you are returned to the line-mode console. Resize the browser window and
the terminal refits automatically.

While any interactive PTY session (e.g. `python`, `vim`, `top`) is running,
press **`Esc`** (or the keybar `Esc` button) to detach immediately and drop
back to the line-mode console — the underlying process is terminated. `Ctrl+C`
(`^C` on the keybar) still sends a normal interrupt into the program.

Everything else (e.g. `ls`, `pwd`, `mkdir`, `cat`, `rm`, `echo`, `git status`,
`pip`, `apt`) is executed on the real host and its output captured and returned
(in a 60-second timeout sandbox).

---

## Hosting web apps & accessing them

`serve` / `gunicorn` register a long-running server process and rconsole
proxies traffic to it:

```
# in the directory you want to publish
ark@rconsole:/srv/mysite$ sudo serve 8000
Hosting '/srv/mysite' at /mysite/8000/
Open: /mysite/8000/
```

- The **folder name** is the basename of the current working directory
  (`/srv/mysite` → `mysite`).
- The sub-server binds to `127.0.0.1:<port>` and is **only** reachable
  through rconsole's proxy at `https://<your-domain>/<folder>/<port>/…`.
- `sudo sprocess` shows the running hosts and PIDs; `sudo kprocess` stops them.

### In production (with a domain)

Deploy rconsole behind a normal web server / reverse proxy on your domain
(see **Production notes**). Then hosted apps are simply available at:

```
https://your-domain.com/mysite/8000/
https://your-domain.com/myapi/9000/
```

No raw ports need to be exposed to the internet.

---

## AI agent (`sudo agent`) — in-console, no PTY

rconsole ships a **native, line-mode AI coding agent** so you get tool-using
AI assistance without fighting a browser-hostile PTY TUI. It calls an
OpenAI-compatible (or Anthropic) API directly and can run tools **inside
rconsole's own host environment** — `bash`, `read_file`, `write_file`,
`list_dir` — all rendered through the normal line console.

### Providers (first match wins)

| Provider       | Key(s)                                  | Base URL (default)                    |
|----------------|-----------------------------------------|---------------------------------------|
| **OpenCode Zen** | `OPENCODE_API_KEY` (alias `ZEN_API_KEY`) | `https://opencode.ai/zen/v1`        |
| Anthropic      | `ANTHROPIC_API_KEY`                     | `https://api.anthropic.com`           |
| OpenAI         | `OPENAI_API_KEY`                        | `https://api.openai.com/v1`           |

Keys are read from your **`sudo env` store** (persisted per user, injected into
every session) or the server process environment. Setting them needs no
redeploy:

```bash
sudo env set OPENCODE_API_KEY=oc-...        # from https://opencode.ai/auth
sudo env set OPENCODE_MODEL=hy3-free        # any Zen /v1/chat/completions model
sudo agent
```

OpenRouter / local OpenAI-compatible servers work by setting `OPENAI_BASE_URL`
(and `OPENAI_MODEL`). **OpenCode Zen note:** the GPT-5.x family on Zen requires
the separate `/v1/responses` API (not yet wrapped here); use a chat/completions
model such as `hy3-free`, `deepseek-v4-flash`, `glm-5.2`, `kimi-k3`, or
`nemotron-3.5-lightning-free`.

### Usage

```bash
sudo agent                       # interactive session (chat persists across commands)
sudo agent "write a flask app and run it"   # one-shot task, then stays open
sudo agent models                # list models from the current provider
sudo agent help                  # show agent slash commands
```

Inside a session, slash commands are available:

```
/help        show help            /clear      forget the conversation
/model <n>   switch model         /provider   show provider/model/base
/keys        list keys (masked)   /models     list provider models
/compact     summarise context    /system     print system prompt
/exit        leave the agent
```

### Robustness

- **Automatic retry/backoff** on `429`/`5xx` and connection errors (pooled
  `requests.Session`).
- **Persistent conversation** across `sudo agent` invocations (stored per tab;
  cleared on `/clear` or `/exit`).
- **Context compaction** — older turns are summarised automatically when the
  transcript grows large, to cap token usage and cost.
- **Token usage report** per turn and per session.
- **Safety guard** — the `bash` tool refuses obviously destructive commands
  (`rm -rf /`, `mkfs`, `dd` on disks, `shutdown`, …); run those directly in the
  console if you truly intend them.

---

## Architecture

```
Browser (xterm.js)
   │  HTTP(S)            REST /api/run  (line-mode commands)
   ▼
Flask app (app.py)
   ├── console.html / console.js   (UI, tabs, terminal rendering)
   ├── /api/run  → interp.py       (command routing & dispatch)
   │                 ├── commands.py   (native/sudo commands + hosting)
   │                 └── real host shell passthrough (subprocess)
   ├── /ws       → ptyhost.py       (WebSocket ⇄ PTY stream)
   │                 ├── Windows : winpty / ConPTY  (_run_pty_win)
   │                 └── Linux/mac: os pty + select (_run_pty_posix)
   └── /<folder>/<port>/…  → proxy (_do_proxy) → hosted app process
```

Key points:

- **Line mode** parses input in `interp.py`. Built-ins live in `commands.py`;
  everything else is executed on the host shell (real `cwd`, venv on `PATH`).
- **Interactive commands** return `{ "pty": true, "cmd": … }`; the client then
  opens a WebSocket to `/ws`, and `ptyhost.py` spawns a shell (`sh -c "<cmd>"`
  on Linux, `cmd.exe /c "<cmd>"` or a POSIX shell on Windows) inside a PTY and
  streams raw bytes both ways.
- **Hosting** launches a real server process (`_static_host.py` for static,
  `_wsgi_host.py` for WSGI) registered in `state.ACTIVE_HOSTS`; the Flask
  proxy forwards matching paths to `http://127.0.0.1:<port>/…`.
- Server state (sessions, tabs, hosted apps) lives in `state.py`; users in
  `data/users.json` (gitignored).

### WebSocket / PTY lifecycle (important)

The one-shot PTY session (`bash -c "nano app.py"`) ends when the child program
exits. The watchdog detects the child's exit and the receive loop stops, the
WebSocket closes, and the client tears the terminal down and returns to the
line console. (This previously left a blank screen on Windows because the
receive loop never noticed the child had exited and the ConPTY `set_size`
arguments were swapped — both are fixed in `ptyhost.py`.)

---

## Production notes

- The built-in server is **Flask's development server** (`app.run(debug=False)`).
  For production, run rconsole behind a real WSGI server (e.g. `gunicorn
  app:app`) and a reverse proxy (nginx / Caddy) terminating TLS on your domain.
- Set strong `RCONSOLE_SECRET` and `RCONSOLE_PEPPER` values.
- Sessions auto-expire after **6 hours** of inactivity (`SESSION_TIMEOUT`).
- Hosted apps are bound to localhost and only exposed via the in-app proxy.

---

## Security model

- Passwords are hashed with `sha256(password + PEPPER)` and stored in
  `data/users.json` (gitignored — do not commit).
- Protected (sudo) commands require an admin account.
- A 6-hour inactivity timeout forces re-login.
- The dev server prints a "do not use in production" warning; use a production
  WSGI server + reverse proxy for real deployments.

---

## Troubleshooting

| Symptom                                                        | Cause / Fix |
|----------------------------------------------------------------|-------------|
| Blank screen after quitting `nano`/`vim`                        | Fixed: receive loop now stops when the child exits; WebSocket closes and the client returns to the console. |
| Terminal text fixed at ~80 columns regardless of window size   | Fixed: ConPTY `set_size(cols, rows)` had swapped args. |
| Hosted app not reachable                                       | Ensure you used `sudo serve`/`sudo gunicorn` (registers the proxy); access `/<folder>/<port>/`. |
| `permission denied` on `serve`/`cpass`/…                       | Prefix with `sudo` and use an admin account (`ark` by default). |
| Changes not taking effect                                      | `debug=False` means no auto-reload — restart the server. For static assets, hard-refresh the browser. |

---

## File reference

See [`structure.md`](./structure.md) for the full file tree and a description
of every module.
