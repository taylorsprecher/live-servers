"""Files Library — HTTP server bootstrap (ThreadingHTTPServer)."""
from http.server import ThreadingHTTPServer

from .config import PORT
from .routes import Handler


def main():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
