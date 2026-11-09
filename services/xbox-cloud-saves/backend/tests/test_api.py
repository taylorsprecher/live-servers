"""Save API through the FastAPI app (default local modes):
round-trip, versioning, user isolation, and the plan's error codes
(Phases 5, 6, 9, 12 exercised end to end)."""

import hashlib
import json
import time

import pytest

import presign_stub

SAVE_BYTES = b"\x00\x01fake-save-data\xff" * 16


def _upload(api_client, headers, name, data=SAVE_BYTES):
    resp = api_client.post("/save", json={"name": name}, headers=headers)
    assert resp.status_code == 200, resp.text
    put = api_client.put(resp.json()["uploadUrl"], content=data)
    assert put.status_code == 200, put.text
    return put.json()


def _download(api_client, headers, name):
    resp = api_client.get("/saves/" + name + "/download-url", headers=headers)
    assert resp.status_code == 200, resp.text
    got = api_client.get(resp.json()["downloadUrl"])
    assert got.status_code == 200, got.text
    return got.content


# ---------------------------------------------------------------- basics

def test_health_discloses_modes(api_client):
    body = api_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["modes"] == {"auth": "xsts", "storage": "local", "presign": "local"}


def test_missing_and_malformed_auth_rejected(api_client):
    assert api_client.get("/saves").status_code == 401
    resp = api_client.get("/saves", headers={"Authorization": "Bearer xyz"})
    assert resp.status_code == 401
    resp = api_client.get("/saves", headers={"Authorization": "XBL3.0 x=a;b.c.d"})
    assert resp.status_code == 401


def test_dev_login_empty_gamertag_400(api_client):
    assert api_client.post("/auth/dev-login", json={"gamertag": "  "}).status_code == 400


# ---------------------------------------------------------------- round trip

def test_upload_list_download_round_trip(api_client, auth_header):
    headers = auth_header("PlayerOne")
    meta = _upload(api_client, headers, "save_slot1.dat")
    assert meta["saveVersion"] == 1
    assert meta["size"] == len(SAVE_BYTES)
    assert meta["sha256"] == hashlib.sha256(SAVE_BYTES).hexdigest()

    listing = api_client.get("/saves", headers=headers).json()
    assert [s["name"] for s in listing["saves"]] == ["save_slot1.dat"]

    assert _download(api_client, headers, "save_slot1.dat") == SAVE_BYTES

    # Phase 5 shape for the download side as well.
    resp = api_client.get("/save", params={"name": "save_slot1.dat"}, headers=headers)
    assert resp.status_code == 200 and "downloadUrl" in resp.json()


def test_upload_bumps_version_and_replaces_bytes(api_client, auth_header):
    headers = auth_header("PlayerOne")
    _upload(api_client, headers, "profile.dat", b"v1")
    meta = _upload(api_client, headers, "profile.dat", b"v2-is-longer")
    assert meta["saveVersion"] == 2
    assert _download(api_client, headers, "profile.dat") == b"v2-is-longer"


def test_rest_alias_upload_url(api_client, auth_header):
    headers = auth_header("PlayerOne")
    resp = api_client.post("/saves/alias.dat/upload-url", headers=headers)
    assert resp.status_code == 200 and "uploadUrl" in resp.json()


def test_delete_round_trip(api_client, auth_header):
    headers = auth_header("PlayerOne")
    _upload(api_client, headers, "gone.dat")
    resp = api_client.delete("/saves/gone.dat", headers=headers)
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    assert api_client.get("/saves", headers=headers).json()["saves"] == []
    assert api_client.delete("/saves/gone.dat", headers=headers).status_code == 404
    assert api_client.get("/saves/gone.dat/download-url",
                          headers=headers).status_code == 404


# ---------------------------------------------------------------- isolation

def test_user_isolation(api_client, auth_header):
    alice = auth_header("Alice")
    bob = auth_header("Bob")
    _upload(api_client, alice, "alice_only.dat")

    assert api_client.get("/saves", headers=bob).json()["saves"] == []
    assert api_client.get("/saves/alice_only.dat/download-url",
                          headers=bob).status_code == 404
    assert api_client.delete("/saves/alice_only.dat",
                             headers=bob).status_code == 404
    # Alice's save is untouched by Bob's attempts.
    assert [s["name"] for s in
            api_client.get("/saves", headers=alice).json()["saves"]] == ["alice_only.dat"]


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("name", ["../evil", "a/b", "bad name!", "", "..", ".hidden..x"])
def test_invalid_save_names_400(api_client, auth_header, name):
    headers = auth_header("PlayerOne")
    resp = api_client.post("/save", json={"name": name}, headers=headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------- transfer tokens

def _craft_transfer_token(op, key, exp):
    payload = {"op": op, "key": key, "exp": exp}
    body = presign_stub._b64encode(json.dumps(payload).encode())
    return body + "." + presign_stub._sign(body)


def test_transfer_token_wrong_operation_401(api_client, auth_header):
    headers = auth_header("PlayerOne")
    _upload(api_client, headers, "ops.dat")
    resp = api_client.get("/saves/ops.dat/download-url", headers=headers)
    get_url = resp.json()["downloadUrl"]
    token = get_url.rsplit("/", 1)[1]
    put = api_client.put(f"/transfer/{token}", content=b"nope")
    assert put.status_code == 401


def test_transfer_token_tampered_401(api_client, auth_header):
    headers = auth_header("PlayerOne")
    resp = api_client.post("/save", json={"name": "t.dat"}, headers=headers)
    token = resp.json()["uploadUrl"].rsplit("/", 1)[1]
    body, sig = token.split(".")
    tampered = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    assert api_client.put(f"/transfer/{tampered}", content=b"x").status_code == 401


def test_transfer_token_expired_401(api_client, login):
    xuid = login("PlayerOne")["xuid"]
    key = f"users/{xuid}/expired.dat"
    token = _craft_transfer_token("put", key, int(time.time()) - 5)
    assert api_client.put(f"/transfer/{token}", content=b"x").status_code == 401


def test_transfer_download_missing_save_404(api_client, login):
    xuid = login("PlayerOne")["xuid"]
    key = f"users/{xuid}/never_uploaded.dat"
    token = _craft_transfer_token("get", key, int(time.time()) + 600)
    assert api_client.get(f"/transfer/{token}").status_code == 404
