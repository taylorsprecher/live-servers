"""
Xbox Cloud Save Platform — Lambda handlers (Increment 2).

The production shape of the Save API: one handler per route behind an
API Gateway HTTP API (see template.yaml at the project root). The
handlers run the same core logic as the local FastAPI app
(backend/app.py) but always with the production-shaped backends:

  * auth_xsts.py   — XSTS JWT structure; signature verified against
                     XSAVE_XSTS_SIGNING_KEY (a local test key here;
                     Microsoft JWKS validation in real production)
  * storage_s3.py  — boto3 S3 (XSAVE_BUCKET; XSAVE_S3_ENDPOINT points
                     it at LocalStack/moto for local runs)
  * presign_s3.py  — boto3 SigV4 pre-signed URLs straight to S3

There is deliberately NO dev-login and NO /transfer handler: clients
arrive with XSTS tokens, and save bytes move directly between the
client and S3 via the pre-signed URLs these handlers issue.

Events are API Gateway HTTP API payload format 2.0. Handlers return
the proxy response shape {"statusCode", "headers", "body"} with the
same JSON bodies and error semantics ({400,401,404,500} ->
{"error": "..."}) as the local app.
"""

import base64
import json

import auth_xsts
import presign_s3
import storage_s3


# ---------------------------------------------------------------- helpers

def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error(status: int, message: str) -> dict:
    return _response(status, {"error": message})


def _header(event: dict, name: str) -> str | None:
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def _json_body(event: dict) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise _HttpError(400, "invalid request: body is not valid JSON")
    if not isinstance(parsed, dict):
        raise _HttpError(400, "invalid request: body must be a JSON object")
    return parsed


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _require_xuid(event: dict) -> str:
    """Phase 2 flow: parse -> validate -> get_xuid (see auth_xsts)."""
    try:
        user_hash, token = auth_xsts.parse_xbl_token(_header(event, "authorization"))
        payload = auth_xsts.validate_xsts_token(token, user_hash)
    except auth_xsts.AuthError as exc:
        raise _HttpError(401, f"unauthorized: {exc.reason}")
    return auth_xsts.get_xuid(payload)


def _validate_name(name) -> str:
    if not isinstance(name, str) or not name:
        raise _HttpError(400, "invalid request: save name is required")
    try:
        return storage_s3.validate_save_name(name)
    except storage_s3.InvalidSaveName as exc:
        raise _HttpError(400, f"invalid request: {exc}")


def _guarded(fn):
    """Run a handler body, mapping domain errors to the plan's codes."""
    try:
        return fn()
    except _HttpError as exc:
        return _error(exc.status, exc.message)
    except Exception:
        return _error(500, "internal server error")


# ---------------------------------------------------------------- handlers

def request_upload_url(event, _context):
    """Phase 5, Endpoint 2: POST /save {name} -> {uploadUrl}."""
    def run():
        xuid = _require_xuid(event)
        name = _validate_name(_json_body(event).get("name"))
        key = storage_s3.key_for(xuid, name)
        return _response(200, {
            "uploadUrl": presign_s3.make_transfer_url("", "put", key),
            "expiresIn": presign_s3.EXPIRES_IN,
        })
    return _guarded(run)


def request_download_url(event, _context):
    """Phase 5, Endpoint 1: GET /save?name=... -> {downloadUrl}."""
    def run():
        xuid = _require_xuid(event)
        params = event.get("queryStringParameters") or {}
        name = _validate_name(params.get("name"))
        if name not in {s["name"] for s in storage_s3.list_saves(xuid)}:
            raise _HttpError(404, "save not found")
        key = storage_s3.key_for(xuid, name)
        return _response(200, {
            "downloadUrl": presign_s3.make_transfer_url("", "get", key),
            "expiresIn": presign_s3.EXPIRES_IN,
        })
    return _guarded(run)


def list_saves(event, _context):
    """GET /saves -> {xuid, saves} for the caller only."""
    def run():
        xuid = _require_xuid(event)
        return _response(200, {"xuid": xuid, "saves": storage_s3.list_saves(xuid)})
    return _guarded(run)


def delete_save(event, _context):
    """DELETE /saves/{name} -> {deleted, name}."""
    def run():
        xuid = _require_xuid(event)
        params = event.get("pathParameters") or {}
        name = _validate_name(params.get("name"))
        try:
            meta = storage_s3.delete_save(xuid, name)
        except storage_s3.SaveNotFound:
            raise _HttpError(404, "save not found")
        return _response(200, {"deleted": True, "name": meta["name"]})
    return _guarded(run)


def health(event, _context):
    """GET /health — liveness for the deployed stack."""
    return _response(200, {
        "status": "ok",
        "service": "xsave-lambda",
        "bucket": storage_s3.get_bucket(),
        "auth": "xsts structure — signature vs configured key",
    })


def on_object_created(event, _context):
    """S3 ObjectCreated trigger (wired in template.yaml).

    In the production flow the client PUTs bytes directly to S3 with a
    pre-signed URL, so the API never sees the upload. This handler
    maintains the per-user metadata index for those objects
    (storage_s3.index_uploaded_object). Not an HTTP handler: returns a
    plain summary for the Lambda log/metrics.
    """
    from urllib.parse import unquote_plus

    indexed = []
    for record in event.get("Records", []):
        key = unquote_plus(record.get("s3", {}).get("object", {}).get("key", ""))
        if not key:
            continue
        meta = storage_s3.index_uploaded_object(key)
        if meta is not None:
            indexed.append(meta["name"])
    return {"indexed": indexed}
