"""Bootstrap used to serve a WSGI app (gunicorn-style) from a user folder.

Usage: python _wsgi_host.py <module:app> <port> <cwd>
Serves the imported WSGI app on 127.0.0.1:<port> using wsgiref.
"""
import sys
from wsgiref.simple_server import make_server

if __name__ == "__main__":
    spec = sys.argv[1]          # e.g. app:app
    port = int(sys.argv[2])
    cwd = sys.argv[3]
    sys.path.insert(0, cwd)
    mod_name, app_name = spec.split(":")
    module = __import__(mod_name)
    app = getattr(module, app_name)
    srv = make_server("127.0.0.1", port, app)
    srv.serve_forever()
