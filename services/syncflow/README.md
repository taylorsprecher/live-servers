# SyncFlow — Backend–Frontend Sync Flow

Deployed on the audited AWS host as part of the live-servers set, behind
the nginx gateway at **http://52.41.84.116:443/syncflow/** and
**https://members.911transedu.net/syncflow/** (host port 18112, loopback
only; the gateway is the public entry point). Added 2026-11-12 at
Taylor's direction: Taylor posted an AI-generated "Backend–Frontend
Sync Flow" diagram and chose "Implement the Backend–Frontend Sync Flow
in a project". Like the other spec builds, this is a demo with invented
demo data only — no real personal data.

- Python 3 stdlib only (`http.server`). No pip installs, no build step.
- One demo profile record (name, display name, bio, location,
  timezone), stored with an integer **version** starting at 1. The
  diagram's v12 → v13 is illustrative; the mechanism is identical.
- State (profile, version, delta history, event stream) persists to a
  JSON file in the `syncflow-data` Docker volume, so it survives
  container restarts.

## The spec, mapped

- **Frontend Client** (the page at `/syncflow/`): a Profile Screen
  (Device A) with optimistic updates, a second device panel (Device B)
  holding its own copy + base version, a Conflict Resolver panel, and
  an Event Listener panel polling the event stream every 2 s.
- **API Gateway**:
  - `GET /syncflow/api/profile` → `{"profile": {...}, "version": n}`
    ("Fetch Latest Profile").
  - `POST /syncflow/api/profile/update` with
    `{"base_version": n, "changes": {field: value}}`
    ("Update Profile" carrying the client's base version).
- **Sync & Conflict Handling**: if `base_version` equals the current
  version, the field-level delta is computed and stored, the version
  increments, and the call returns
  `{"profile", "version", "delta"}` with HTTP 200. If it does not
  match, the call returns HTTP **409** with
  `{"server_profile", "server_version", "your_base_version"}` — the
  data the Conflict Resolver needs. "Keep mine" re-submits against the
  server version; "Use server" discards the local edits and refreshes.
  A save whose changes match the stored values is a no-op: 200 with an
  empty delta, no version bump, no events.
- **Client Updates**: on 200 the device applies the stored delta; on
  409 the Conflict Resolver is shown; Refresh Profile re-fetches.
- **Event Bus / Event Stream**: `GET /syncflow/api/events` returns the
  stream (newest last; each event `{"seq", "type", "version",
  "timestamp", "detail"}`). `DELTA_CREATED` and `PROFILE_UPDATED` are
  emitted on every successful update, `CONFLICT_DETECTED` on every
  409. Events are appended only when the thing actually happens — the
  diagram's ROLLBACK is not emitted, because no path in this demo
  performs a rollback (discarding local edits on "Use server" is a
  client-side refresh, not a server rollback).
- `GET /syncflow/api/deltas` → the stored delta history.
- `POST /syncflow/api/reset` → demo affordance: restores the demo
  profile at version 1 and clears deltas and events.
- Health: `GET /syncflow/health` → `{"status": "ok"}`.

## Deploy

Targeted deploy: `.github/workflows/deploy-syncflow.yml` rsyncs the
repo and runs `ops/deploy-syncflow.sh`, which builds/recreates ONLY
the syncflow container, then force-recreates the gateway so it loads
the current locations-common.conf (the /syncflow route lives there).
`services/syncflow/e2e_test.py` runs against the public gateway at the
end of the workflow and exercises the full flow (update → delta →
events → stale update → 409 → reset).
