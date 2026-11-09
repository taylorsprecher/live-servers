"""Lambda handlers behind API Gateway-shaped events, against moto S3.

Exercises the full production loop locally: handler issues a SigV4
pre-signed URL -> bytes land directly in S3 -> ObjectCreated handler
indexes them -> list/download/delete handlers see them. Plus auth and
isolation failures (Phases 2, 5, 6, 12).
"""

import json
from urllib.parse import urlparse

import auth_xsts
from functions import handlers


def _event(method="POST", body=None, headers=None, query=None, path_params=None):
    return {
        "requestContext": {"http": {"method": method}},
        "headers": headers or {},
        "queryStringParameters": query,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


def _headers_for(gamertag):
    user_hash, token, _ = auth_xsts.issue_dev_token(gamertag)
    return {"Authorization": f"XBL3.0 x={user_hash};{token}"}


def _body(resp):
    return json.loads(resp["body"])


def _key_from_presigned(url: str) -> str:
    # moto/boto3 virtual-host style: https://<bucket>.s3.<region>.amazonaws.com/<key>
    return urlparse(url).path.lstrip("/")


def _simulate_client_put(s3, url, data):
    """What the console client does with a pre-signed upload URL:
    PUT the bytes straight to S3 (here: the same boto3 client call the
    HTTP PUT would perform against moto)."""
    key = _key_from_presigned(url)
    s3.get_client().put_object(Bucket=s3.get_bucket(), Key=key, Body=data)
    return key


def _s3_event(key):
    return {"Records": [{"s3": {"object": {"key": key}}}]}


# ---------------------------------------------------------------- auth

def test_handlers_reject_missing_and_bad_auth(s3):
    resp = handlers.request_upload_url(_event(body={"name": "x.dat"}), None)
    assert resp["statusCode"] == 401
    assert "missing token" in _body(resp)["error"]

    resp = handlers.list_saves(_event("GET", headers={"Authorization": "XBL3.0 x=a;b.c.d"}), None)
    assert resp["statusCode"] == 401


def test_health_handler(s3):
    resp = handlers.health(_event("GET"), None)
    assert resp["statusCode"] == 200 and _body(resp)["status"] == "ok"


# ---------------------------------------------------------------- full loop

def test_full_production_loop(s3):
    headers = _headers_for("PlayerOne")
    data = b"lambda-round-trip-bytes"

    # 1. Ask for an upload URL.
    resp = handlers.request_upload_url(
        _event(body={"name": "slot1.dat"}, headers=headers), None)
    assert resp["statusCode"] == 200, resp
    upload_url = _body(resp)["uploadUrl"]
    assert "X-Amz-Signature" in upload_url and "X-Amz-Expires=900" in upload_url
    key = _key_from_presigned(upload_url)
    # The URL must be scoped to the caller's own users/{xuid}/ prefix.
    user_hash, token = auth_xsts.parse_xbl_token(headers["Authorization"])
    xuid = auth_xsts.get_xuid(auth_xsts.validate_xsts_token(token, user_hash))
    assert key == f"users/{xuid}/slot1.dat"

    # 2. Bytes land directly in S3; the ObjectCreated handler indexes.
    _simulate_client_put(s3, upload_url, data)
    result = handlers.on_object_created(_s3_event(key), None)
    assert result == {"indexed": ["slot1.dat"]}

    # 3. List shows the save with computed integrity metadata.
    resp = handlers.list_saves(_event("GET", headers=headers), None)
    saves = _body(resp)["saves"]
    assert len(saves) == 1 and saves[0]["name"] == "slot1.dat"
    assert saves[0]["size"] == len(data)

    # 4. Download URL points at the same object; bytes verify.
    resp = handlers.request_download_url(
        _event("GET", headers=headers, query={"name": "slot1.dat"}), None)
    assert resp["statusCode"] == 200
    download_key = _key_from_presigned(_body(resp)["downloadUrl"])
    assert download_key == key
    import storage_s3
    assert storage_s3.get_bytes(download_key) == data

    # 5. Delete removes it.
    resp = handlers.delete_save(
        _event("DELETE", headers=headers, path_params={"name": "slot1.dat"}), None)
    assert resp["statusCode"] == 200
    resp = handlers.list_saves(_event("GET", headers=headers), None)
    assert _body(resp)["saves"] == []


def test_lambda_user_isolation(s3):
    alice = _headers_for("Alice")
    bob = _headers_for("Bob")
    resp = handlers.request_upload_url(
        _event(body={"name": "alice.dat"}, headers=alice), None)
    key = _simulate_client_put(s3, _body(resp)["uploadUrl"], b"alice-bytes")
    handlers.on_object_created(_s3_event(key), None)

    resp = handlers.list_saves(_event("GET", headers=bob), None)
    assert _body(resp)["saves"] == []
    resp = handlers.request_download_url(
        _event("GET", headers=bob, query={"name": "alice.dat"}), None)
    assert resp["statusCode"] == 404
    resp = handlers.delete_save(
        _event("DELETE", headers=bob, path_params={"name": "alice.dat"}), None)
    assert resp["statusCode"] == 404
    # Alice's save intact.
    resp = handlers.list_saves(_event("GET", headers=alice), None)
    assert len(_body(resp)["saves"]) == 1


def test_lambda_validation_errors(s3):
    headers = _headers_for("PlayerOne")
    resp = handlers.request_upload_url(
        _event(body={"name": "../evil"}, headers=headers), None)
    assert resp["statusCode"] == 400
    resp = handlers.request_upload_url(_event(body={}, headers=headers), None)
    assert resp["statusCode"] == 400
    resp = handlers.request_download_url(
        _event("GET", headers=headers, query={"name": "ghost.dat"}), None)
    assert resp["statusCode"] == 404
