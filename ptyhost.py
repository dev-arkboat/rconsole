"""PTY-backed interactive terminal sessions streamed over a websocket.

This module is now a thin bridge between the websocket protocol and the
persistent :mod:`termsess` sessions. A session survives websocket disconnects
(refresh / tab switch) and is only torn down when the client sends an explicit
``kill`` message (the user closing the tab) so that long-running processes such
as a Discord bot keep running until the user really wants them gone.
"""
import json

import state
import termsess
from termsess import SESSIONS, SESSIONS_LOCK, TermSession


def run_pty(ws, cmd, cwd, cols=80, rows=24, sid=None, tab_id=None):
    """Backwards-compatible alias for :func:`attach`."""
    attach(ws, sid, tab_id, cmd, cwd, cols, rows)


def attach(ws, sid, tab_id, cmd, cwd, cols=80, rows=24):
    """Attach (or create and attach) a persistent terminal session.

    Protocol (client -> server):
      {"t": "data", "d": "<keystrokes>"}   raw terminal input
      {"t": "resize", "cols": N, "rows": N}  terminal resize
      {"t": "kill"}                          terminate this session's processes

    Server -> client is raw terminal output (str), and on (re)attach the full
    backlog is replayed so a returning user sees where they left off.
    """
    if sid is None or tab_id is None:
        return

    created = False
    with SESSIONS_LOCK:
        session = SESSIONS.get((sid, tab_id))
        if session is None:
            if not cmd:
                # Nothing to run and no existing session: nothing to do.
                return
            session = TermSession(sid, tab_id, cmd, cwd, cols, rows)
            SESSIONS[(sid, tab_id)] = session
            created = True
        else:
            # Refresh geometry on every (re)attach.
            try:
                session.resize(cols, rows)
            except Exception:
                pass

    if created:
        # Persist the tab so it survives a server restart / fresh login.
        try:
            sess = state.get_session(sid)
            if sess:
                state.set_terminal(sess, sid, tab_id, cmd, cwd)
        except Exception:
            pass

    session.attach(ws)
    try:
        while True:
            try:
                msg = ws.receive(timeout=0.3)
            except Exception:
                # ConnectionClosed (client gone / refresh) -> keep the session.
                break
            if msg is None:
                continue
            try:
                obj = json.loads(msg)
            except (ValueError, TypeError):
                try:
                    session.write(msg)
                except Exception:
                    break
                continue
            t = obj.get("t")
            if t == "resize":
                try:
                    session.resize(int(obj.get("cols", 80)), int(obj.get("rows", 24)))
                except Exception:
                    pass
            elif t == "data":
                try:
                    session.write(obj.get("d", ""))
                except Exception:
                    pass
            elif t == "kill":
                # User explicitly closed the tab: stop its processes and remove
                # the session + server-side tab so nothing lingers.
                session.kill()
                with SESSIONS_LOCK:
                    SESSIONS.pop((sid, tab_id), None)
                state.remove_tab(sid, tab_id)
                break
    finally:
        # Detach only: the session lives on so the user can return to it.
        session.detach(ws)
