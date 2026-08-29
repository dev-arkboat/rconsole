"""Bootstrap used to serve a WSGI app (gunicorn-style) from a user folder.

Usage (wsgiref fallback, invoked by `sudo serve`/`sudo gunicorn`):
    python _wsgi_host.py <module:app> <port> <cwd> [prefix]

Usage (real gunicorn, preferred on Linux/macOS):
    gunicorn _wsgi_host:application -b 127.0.0.1:<port> --chdir <cwd>
with env: RCONSOLE_SPEC=<module:app> RCONSOLE_CWD=<cwd> RCONSOLE_PREFIX=/<folder>/<port>

The app is mounted under the rconsole proxy prefix (`/<folder>/<port>`). We
inject SCRIPT_NAME into the WSGI environ so the hosted app (e.g. Flask) both
matches its routes under that prefix AND generates prefixed URLs — otherwise a
Flask app that defines `/login` and emits `url_for('login')` would produce an
absolute `/login`, which the browser would send to the *main* rconsole site and
collide with its own routes.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _make_app(spec, cwd, prefix):
    # Make both this module and the user's project importable regardless of the
    # server's working directory.
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    if cwd and cwd not in sys.path:
        sys.path.insert(0, cwd)

    mod_name, app_name = spec.split(":")
    module = __import__(mod_name)
    app = getattr(module, app_name)

    prefix = (prefix or "").rstrip("/")
    if not prefix:
        return app

    # APPLICATION_ROOT makes url_for() emit the prefix even outside a request
    # context; SCRIPT_NAME (injected per-request below) makes the routing layer
    # and request-time url_for() agree on the mount point.
    try:
        app.config["APPLICATION_ROOT"] = prefix
    except Exception:
        pass

    state = {"prefix": prefix}

    def app_with_script_name(environ, start_response):
        environ["SCRIPT_NAME"] = state["prefix"]
        return app(environ, start_response)

    return app_with_script_name


# --------------------------------------------------------------------------
# gunicorn entry point: `gunicorn _wsgi_host:application ...` imports `application`
# below and calls it per request. The real app is built lazily (once) from the
# env vars rconsole sets when launching gunicorn.
# --------------------------------------------------------------------------
_application = None


def _load_from_env():
    global _application
    if _application is not None:
        return _application
    spec = os.environ.get("RCONSOLE_SPEC")
    cwd = os.environ.get("RCONSOLE_CWD")
    prefix = os.environ.get("RCONSOLE_PREFIX", "")
    if spec:
        try:
            _application = _make_app(spec, cwd, prefix)
        except Exception as e:  # surface a readable error instead of a silent 500
            _application = _error_app(
                "rconsole: failed to load %s from %s: %s" % (spec, cwd, e)
            )
    return _application


def _error_app(message):
    body = message.encode("utf-8")

    def app(environ, start_response):
        start_response("500 Internal Server Error",
                       [("Content-Type", "text/plain; charset=utf-8")])
        return [body]

    return app


def application(environ, start_response):
    app = _load_from_env()
    if app is None:
        return _error_app(
            "rconsole: hosted app not configured (RCONSOLE_SPEC missing)"
        )(environ, start_response)
    return app(environ, start_response)


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    spec = sys.argv[1]                       # e.g. app:app
    port = int(sys.argv[2])
    cwd = sys.argv[3]
    folder = os.path.basename(cwd.rstrip("/\\")) or "root"
    prefix = sys.argv[4] if len(sys.argv) > 4 else f"/{folder}/{port}"

    app = _make_app(spec, cwd, prefix)
    srv = make_server("127.0.0.1", port, app)
    srv.serve_forever()
