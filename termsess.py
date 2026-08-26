"""Persistent terminal sessions keyed by (sid, tab_id).

A session owns a real PTY process and a rolling output buffer. Client
websockets attach/detach without killing the process; only an explicit kill
(e.g. the user closing the tab) terminates it. This lets a command such as a
long-running Discord bot survive page refreshes and tab switches, and lets the
user re-attach to see where they left off.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

BUFFER_MAX = 200 * 1024  # characters kept per session

SESSIONS = {}              # (sid, tab_id) -> TermSession
SESSIONS_LOCK = threading.Lock()


def _build_env():
    env = os.environ.copy()
    venv_dir = Path(sys.executable).parent
    path = env.get("PATH", "")
    if str(venv_dir) not in path.split(os.pathsep):
        env["PATH"] = str(venv_dir) + os.pathsep + path
    return env


def get(sid, tab_id):
    return SESSIONS.get((sid, tab_id))


def kill_all_for(sid):
    with SESSIONS_LOCK:
        keys = [k for k in SESSIONS if k[0] == sid]
        for k in keys:
            SESSIONS.pop(k, None)
    for k in keys:
        try:
            SESSIONS[k].kill()
        except Exception:
            pass


class TermSession:
    def __init__(self, sid, tab_id, cmd, cwd, cols=80, rows=24):
        self.sid = sid
        self.tab_id = tab_id
        self.cmd = cmd
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.buffer = ""
        self.clients = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.alive = True
        self.exited = False
        self._spawn()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ------------------------------------------------------------------ spawn
    def _spawn(self):
        if sys.platform == "win32":
            self._spawn_win()
        else:
            self._spawn_posix()

    def _spawn_win(self):
        from winpty import PTY
        from interp import _detect_shell
        shell_exe, flag = _detect_shell()
        cmdline = f'{flag} "{self.cmd.replace(chr(34), "\\" + chr(34))}"' if self.cmd else None
        env = _build_env()
        env_str = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"
        pty = PTY(self.cols, self.rows)
        pty.spawn(shell_exe, cmdline=cmdline, cwd=self.cwd, env=env_str)
        self.pty = pty
        self.master_fd = None
        self.proc = None

    def _spawn_posix(self):
        import fcntl
        import pty as pty_mod
        import select
        import signal
        import struct
        import subprocess
        import termios
        from interp import _detect_shell
        shell_exe, flag = _detect_shell()
        argv = [shell_exe, flag, self.cmd] if self.cmd else [shell_exe]
        master_fd, slave_fd = pty_mod.openpty()
        try:
            winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass
        env = _build_env()
        proc = subprocess.Popen(
            argv, cwd=self.cwd, env=env,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True, start_new_session=True,
        )
        os.close(slave_fd)
        self.master_fd = master_fd
        self.proc = proc
        self._posix = (fcntl, select, signal, termios, struct)

    # ----------------------------------------------------------------- reader
    def _reader(self):
        if sys.platform == "win32":
            self._reader_win()
        else:
            self._reader_posix()

    def _reader_win(self):
        while not self.stop.is_set() and self.pty.isalive():
            try:
                data = self.pty.read()
            except Exception:
                break
            if data:
                self._append(data)
                self._broadcast(data)
            else:
                time.sleep(0.02)

    def _reader_posix(self):
        fcntl, select, signal, termios, struct = self._posix
        while not self.stop.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not r:
                if self.proc.poll() is not None:
                    try:
                        tail = os.read(self.master_fd, 65536)
                    except OSError:
                        tail = b""
                    if tail:
                        s = tail.decode("utf-8", "replace")
                        self._append(s)
                        self._broadcast(s)
                    break
                continue
            try:
                data = os.read(self.master_fd, 65536)
            except OSError:
                break
            if not data:
                break
            s = data.decode("utf-8", "replace")
            self._append(s)
            self._broadcast(s)

    # --------------------------------------------------------------- watchdog
    def _watchdog(self):
        if sys.platform == "win32":
            while not self.stop.is_set() and self.pty.isalive():
                time.sleep(0.2)
        else:
            try:
                self.proc.wait()
            except Exception:
                pass
        self.stop.set()
        self.alive = False
        self.exited = True
        try:
            self._append("\r\n*** terminal session ended ***\r\n")
        except Exception:
            pass
        # Tell the client the process has exited so it can automatically return
        # to the line console instead of leaving the user stuck in the terminal
        # view (the "ws") with a dead session.
        try:
            self._broadcast(json.dumps({"t": "exit"}))
        except Exception:
            pass
        # A finished session can't be usefully re-attached, so tear it down
        # server-side too (drop the in-memory session and the persisted tab).
        try:
            import state as _state
            with SESSIONS_LOCK:
                SESSIONS.pop((self.sid, self.tab_id), None)
            _state.remove_tab(self.sid, self.tab_id)
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                self.pty.cancel_io()
            else:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------- lifecycle
    def _append(self, s):
        self.buffer += s
        if len(self.buffer) > BUFFER_MAX:
            self.buffer = self.buffer[-BUFFER_MAX:]

    def _broadcast(self, data):
        with self.lock:
            for ws in list(self.clients):
                try:
                    ws.send(data)
                except Exception:
                    try:
                        self.clients.remove(ws)
                    except Exception:
                        pass

    def attach(self, ws):
        with self.lock:
            if self.buffer:
                try:
                    ws.send(self.buffer)
                except Exception:
                    pass
            if ws not in self.clients:
                self.clients.append(ws)

    def detach(self, ws):
        with self.lock:
            if ws in self.clients:
                self.clients.remove(ws)
        # Intentionally does NOT kill the process: a disconnect (refresh / tab
        # switch) must preserve the session so the user can return to it.

    def write(self, data):
        try:
            if sys.platform == "win32":
                self.pty.write(data)
            else:
                os.write(self.master_fd, data.encode("utf-8", "replace"))
        except Exception:
            pass

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        try:
            if sys.platform == "win32":
                self.pty.set_size(int(cols), int(rows))
            else:
                fcntl, select, signal, termios, struct = self._posix
                winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def kill(self):
        """Terminate the whole process group and tear down the session."""
        self.stop.set()
        try:
            if sys.platform == "win32":
                try:
                    self.pty.write("\x03")
                except Exception:
                    pass
                try:
                    self.pty.write("exit\r\n")
                except Exception:
                    pass
                try:
                    self.pty.cancel_io()
                except Exception:
                    pass
            else:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
        except Exception:
            pass
        self.alive = False
        self.exited = True
