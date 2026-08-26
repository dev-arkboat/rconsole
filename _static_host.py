"""Bootstrap static file server for `serve` (serves a real materialized dir)."""
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

if __name__ == "__main__":
    port = int(sys.argv[1])
    directory = sys.argv[2]
    os.chdir(directory)
    handler = SimpleHTTPRequestHandler
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.serve_forever()
