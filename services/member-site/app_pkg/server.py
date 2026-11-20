"""HTTP server bootstrap."""
from http.server import ThreadingHTTPServer

from .config import BASE, DB_PATH, PORT
from .routes import Handler
from .store import init_db


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"member-site listening on :{PORT} base={BASE} db={DB_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
