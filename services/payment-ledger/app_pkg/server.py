"""HTTP server bootstrap — the monolith's __main__ block, moved
verbatim in behavior: DATABASE_URL is required, schema application
is retried up to 30 times (2s apart, resetting the shared
connection between attempts), the ingestion worker runs as a
daemon thread, and the server listens on 0.0.0.0:PORT."""

import threading
import time
from http.server import ThreadingHTTPServer

from . import authnet, store
from .config import BASE, DATABASE_URL, PORT
from .ledger import worker_loop
from .renewal import renewal_loop
from .routes import Handler
from .store import apply_schema


def main():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is required")
    for attempt in range(30):
        try:
            apply_schema()
            break
        except Exception as exc:
            print(f"schema apply attempt {attempt + 1} failed: {exc}", flush=True)
            try:
                store._conn = None
            except Exception:
                pass
            time.sleep(2)
    else:
        raise SystemExit("could not apply schema after 30 attempts")
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=renewal_loop, daemon=True).start()
    try:
        authnet.log_state()
    except Exception as exc:  # the state line must never block boot
        print(f"payment-ledger authnet: state check failed: {exc}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"payment-ledger listening on :{PORT} base={BASE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
