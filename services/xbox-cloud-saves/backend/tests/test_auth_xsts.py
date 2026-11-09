"""Phase 2 auth: real XSTS JWT structure, local test-key signatures."""

import base64
import json
import time

import pytest

import auth_stub
import auth_xsts


def _payload_of(token: str) -> dict:
    body = token.split(".")[1]
    pad = "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body + pad))


def _resign(token: str, mutate) -> str:
    payload = _payload_of(token)
    mutate(payload)
    return auth_xsts.sign_token(payload)


# ---------------------------------------------------------------- happy path

def test_issue_and_validate_round_trip():
    user_hash, token, exp = auth_xsts.issue_dev_token("PlayerOne")
    payload = auth_xsts.validate_xsts_token(token, user_hash)
    assert exp > int(time.time())
    xuid = auth_xsts.get_xuid(payload)
    assert xuid.startswith("2535") and len(xuid) == 16 and xuid.isdigit()
    assert auth_xsts.get_user_hash(payload) == user_hash


def test_xuid_matches_increment1_stub_derivation():
    # A gamertag must keep the same XUID (storage prefix) across increments.
    _, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    xuid_new = auth_xsts.get_xuid(auth_xsts.validate_xsts_token(token))
    assert xuid_new == auth_stub.get_xuid({"sub": "PlayerOne"})


def test_distinct_gamertags_distinct_xuids():
    _, token_a, _ = auth_xsts.issue_dev_token("Alice")
    _, token_b, _ = auth_xsts.issue_dev_token("Bob")
    assert auth_xsts.get_xuid(auth_xsts.validate_xsts_token(token_a)) != \
        auth_xsts.get_xuid(auth_xsts.validate_xsts_token(token_b))


# ---------------------------------------------------------------- rejections

def test_expired_token_rejected():
    user_hash, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    expired = _resign(token, lambda p: p.update(exp=int(time.time()) - 10))
    with pytest.raises(auth_xsts.AuthError) as exc:
        auth_xsts.validate_xsts_token(expired, user_hash)
    assert exc.value.reason == "expired token"


def test_tampered_payload_rejected():
    user_hash, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    head, _body, sig = token.split(".")
    payload = _payload_of(token)
    payload["DisplayClaims"]["xui"][0]["xid"] = "2535000000000001"
    forged_body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).rstrip(b"=").decode()
    with pytest.raises(auth_xsts.AuthError) as exc:
        auth_xsts.validate_xsts_token(f"{head}.{forged_body}.{sig}", user_hash)
    assert exc.value.reason == "invalid token"


def test_wrong_signing_key_rejected(monkeypatch):
    user_hash, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    payload = _payload_of(token)
    monkeypatch.setattr(auth_xsts, "_SIGNING_KEY", b"a-different-key")
    forged = auth_xsts.sign_token(payload)
    monkeypatch.undo()
    with pytest.raises(auth_xsts.AuthError) as exc:
        auth_xsts.validate_xsts_token(forged, user_hash)
    assert exc.value.reason == "invalid token"


def test_user_hash_mismatch_rejected():
    _, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    other_hash, _, _ = auth_xsts.issue_dev_token("Mallory")
    with pytest.raises(auth_xsts.AuthError) as exc:
        auth_xsts.validate_xsts_token(token, other_hash)
    assert exc.value.reason == "invalid token"


def test_wrong_issuer_rejected():
    user_hash, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    bad = _resign(token, lambda p: p.update(iss="https://evil.example.com"))
    with pytest.raises(auth_xsts.AuthError) as exc:
        auth_xsts.validate_xsts_token(bad, user_hash)
    assert exc.value.reason == "invalid token"


def test_missing_display_claims_rejected():
    user_hash, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    bad = _resign(token, lambda p: p.pop("DisplayClaims"))
    with pytest.raises(auth_xsts.AuthError) as exc:
        auth_xsts.validate_xsts_token(bad, user_hash)
    assert exc.value.reason == "malformed token"


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d", "!.!.!", "e30.e30.e30"])
def test_malformed_tokens_rejected(token):
    with pytest.raises(auth_xsts.AuthError):
        auth_xsts.validate_xsts_token(token)


def test_unsigned_increment1_token_no_longer_valid():
    # The Increment 1 stub token (unsigned base64 JSON) must fail the
    # real structure checks — that is the point of this increment.
    _hash, stub_token, _ = auth_stub.issue_dev_token("PlayerOne")
    with pytest.raises(auth_xsts.AuthError):
        auth_xsts.validate_xsts_token(stub_token)


# ---------------------------------------------------------------- header parsing

def test_parse_xbl_token_ok():
    user_hash, token, _ = auth_xsts.issue_dev_token("PlayerOne")
    parsed_hash, parsed_token = auth_xsts.parse_xbl_token(f"XBL3.0 x={user_hash};{token}")
    assert parsed_hash == user_hash and parsed_token == token


@pytest.mark.parametrize("header", [None, "", "Bearer abc", "XBL3.0 x=onlyhash",
                                    "XBL3.0 x=;token", "XBL3.0 x=hash;"])
def test_parse_xbl_token_rejects_bad_headers(header):
    with pytest.raises(auth_xsts.AuthError):
        auth_xsts.parse_xbl_token(header)
