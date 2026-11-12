#!/usr/bin/env python3
"""End-to-end test of the SyncFlow service through a base URL.

Usage: python3 e2e_test.py <base>   e.g.
       python3 e2e_test.py http://52.41.84.116:443/syncflow

Exercises the whole spec flow and prints a transcript:
  reset -> GET profile (v1) -> update with the correct base (200,
  version bumps, delta holds exactly the changed fields) -> event
  stream shows DELTA_CREATED + PROFILE_UPDATED -> update with the now
  stale base (409, resolver data in the body) -> event stream shows
  CONFLICT_DETECTED -> reset leaves the demo pristine.

Stdlib only. Exits non-zero on the first failed expectation.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:18112/syncflow"


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        sys.exit(1)


def main():
    code, _ = call("POST", "/api/reset")
    check("reset", code == 200, code)

    code, data = call("GET", "/api/profile")
    check("GET profile 200", code == 200, code)
    check("profile at version 1 after reset", data["version"] == 1, data["version"])
    v = data["version"]
    print("profile:", json.dumps(data["profile"]))

    new_bio = "E2E bio - updated through the gateway."
    code, data = call(
        "POST", "/api/profile/update", {"base_version": v, "changes": {"bio": new_bio}}
    )
    check("update with correct base -> 200", code == 200, code)
    check("version bumped by 1", data["version"] == v + 1, data["version"])
    check(
        "delta contains exactly the changed field",
        data["delta"] == {"bio": {"from": data["delta"]["bio"]["from"], "to": new_bio}}
        and list(data["delta"].keys()) == ["bio"],
        json.dumps(data["delta"]),
    )

    code, data = call("GET", "/api/events")
    types = [e["type"] for e in data["events"]]
    check("events show DELTA_CREATED", "DELTA_CREATED" in types, types)
    check("events show PROFILE_UPDATED", "PROFILE_UPDATED" in types, types)

    code, data = call("GET", "/api/deltas")
    check("delta history holds the stored delta", len(data["deltas"]) == 1, len(data["deltas"]))

    code, data = call(
        "POST", "/api/profile/update", {"base_version": v, "changes": {"location": "Stale City"}}
    )
    check("update with stale base -> 409", code == 409, code)
    check("409 carries server_version", data.get("server_version") == v + 1, data.get("server_version"))
    check("409 carries your_base_version", data.get("your_base_version") == v, data.get("your_base_version"))
    check(
        "409 carries server_profile (resolver data)",
        isinstance(data.get("server_profile"), dict) and data["server_profile"]["bio"] == new_bio,
    )

    code, data = call("GET", "/api/events")
    types = [e["type"] for e in data["events"]]
    check("events show CONFLICT_DETECTED", "CONFLICT_DETECTED" in types, types)

    code, data = call("POST", "/api/reset")
    check("final reset -> profile back at version 1", code == 200 and data["version"] == 1)
    print("E2E OK")


if __name__ == "__main__":
    main()
