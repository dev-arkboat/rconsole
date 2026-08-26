"""Command line parsing and execution dispatcher.

Built-in console commands are handled in commands.py. Anything else is executed
on the REAL host via a shell subprocess (real cwd, timeout, venv on PATH).
"""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import commands

PASS_THROUGH_TIMEOUT = 60

# Commands that need a real, interactive terminal (full-screen TUIs, REPLs,
# pagers, remote shells, ...). These are run through a PTY rather than the
# captured stdout passthrough so that programs like nano/vim/python work.
INTERACTIVE = {
    "nano", "vim", "vi", "emacs", "nvim",
    "less", "more", "man", "most",
    "top", "htop", "btm", "glances", "watch",
    "python", "python3", "py", "ipython", "node", "irb", "lua", "php",
    "bash", "sh", "zsh", "fish", "powershell", "pwsh", "cmd", "cmd.exe",
    "ssh", "telnet", "tmux", "screen", "lynx", "w3m", "mutt", "irssi",
    "htop", "fzf", "ranger", "nnn", "mc", "git",
}


def _is_interactive(tokens):
    if not tokens:
        return False
    name = tokens[0]
    # Strip a leading path / extension for the comparison.
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.rsplit(".", 1)[0].lower()
    if base in INTERACTIVE:
        return True
    # Heuristics: git/ssh subcommands that drop into a pager or editor.
    if base in ("git",) and len(tokens) > 1 and tokens[1] in (
        "log", "diff", "show", "status", "blame", "branch", "-pager-err",
    ):
        return True
    return False


def tokenize(line):
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _detect_shell():
    """Prefer a POSIX shell (sh/bash) for a real Linux-console feel, then
    common Git Bash locations, then fall back to cmd.exe on Windows.
    The shell is invoked explicitly (we never use shell=True)."""
    candidates = [
        "sh", "bash",
        r"C:\Program Files\Git\bin\sh.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\sh.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for c in candidates:
        c_p = Path(c)
        if c_p.is_absolute():
            if c_p.exists():
                return str(c_p), "-c"
        elif shutil.which(c):
            return shutil.which(c), "-c"
    if os.name == "nt":
        return "cmd.exe", "/c"
    return "sh", "-c"


def _passthrough(ctx, host_cmd):
    """Run a command string on the host using an explicit shell."""
    env = os.environ.copy()
    venv_dir = Path(sys.executable).parent
    path = env.get("PATH", "")
    if str(venv_dir) not in path.split(os.pathsep):
        env["PATH"] = str(venv_dir) + os.pathsep + path

    shell_exe, flag = _detect_shell()
    cwd = ctx["tab"]["cwd"]
    try:
        proc = subprocess.run(
            [shell_exe, flag, host_cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=PASS_THROUGH_TIMEOUT,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"command timed out after {PASS_THROUGH_TIMEOUT}s"
    except Exception as e:
        return f"error: {e}"
    out = proc.stdout or ""
    err = proc.stderr or ""
    return (out + err).rstrip("\n")


def process(tab, username, is_admin, sid, raw):
    """Process a line of input for a tab.

    Returns a dict with some of: output, clear, action, update_session, prompt.
    """
    line = raw.rstrip("\n")

    # Interactive command awaiting input?
    if tab.get("gen") is not None:
        gen = tab["gen"]
        try:
            r = gen.send(line)
        except StopIteration as e:
            tab["gen"] = None
            tab["prompt"] = None
            return _normalize(e.value)
        return _normalize_gen(r)

    if line.strip() == "":
        return {}

    tokens = tokenize(line)
    if not tokens:
        return {}

    sudo = False
    host_cmd = line.strip()
    if tokens[0] == "sudo":
        sudo = True
        # remove the leading "sudo " for the host command
        if host_cmd.lower().startswith("sudo "):
            host_cmd = host_cmd[5:].lstrip()
        tokens = tokens[1:]
        if not tokens:
            return {"output": "usage: sudo <command>"}

    name = tokens[0]
    args = tokens[1:]

    ctx = {
        "username": username,
        "is_admin": is_admin,
        "tab": tab,
        "sid": sid,
        "sudo": sudo,
        "raw": raw,
    }

    # Console-native built-in?
    if name in commands.COMMANDS:
        if name in commands.PROTECTED and not sudo:
            return {"output": f"Permission denied: '{name}' requires sudo. Try: sudo {name}"}
        if sudo and not is_admin:
            return {"output": f"{username} is not in the sudoers file. This incident will be reported."}
        fn = commands.COMMANDS[name]
        try:
            result = fn(ctx, args)
        except Exception as e:
            return {"output": f"{name}: error: {e}"}
        if hasattr(result, "send") and hasattr(result, "__next__"):
            try:
                r = next(result)
                tab["gen"] = result
                tab["prompt"] = None
                return _normalize_gen(r)
            except StopIteration as e:
                return _normalize(e.value)
        return _normalize(result)

    # Everything else -> run on the real host shell.
    if _is_interactive(tokens):
        # Hand off to a real PTY so interactive programs work.
        return {"pty": True, "cmd": host_cmd}
    return {"output": _passthrough(ctx, host_cmd)}


def _normalize(result):
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return {"output": str(result)}


def _normalize_gen(r):
    """Normalize a value yielded by an interactive generator.

    A generator may yield either a plain prompt string or a dict carrying a
    prompt plus optional fields (output, clear) so an interactive command (e.g.
    an in-console editor) can render content between user inputs.
    """
    if isinstance(r, dict):
        r.setdefault("prompt", "")
        return r
    return {"prompt": str(r)}
