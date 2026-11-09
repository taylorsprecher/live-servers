"""
Xbox Cloud Save Platform — local development API.

Implements the Save API surface from the production plan (Phases 2, 3,
5, 6, 7) with pluggable backends selected by environment variables:

  XSAVE_AUTH      "xsts"  (default) auth_xsts.py — real XSTS JWT
                          structure, signature checked against a local
                          test key
                  "stub"  auth_stub.py — Increment 1 unsigned fake
                          tokens (kept for comparison/regression)
  XSAVE_STORAGE   "local" (default) storage_stub.py — local filesystem
                  "s3"    storage_s3.py — boto3 against S3 /
                          LocalStack / moto (XSAVE_S3_ENDPOINT,
                          XSAVE_BUCKET)
  XSAVE_PRESIGN   "local" (default) presign_stub.py — HMAC capability
                          tokens served by this app at /transfer/{token}
                  "s3"    presign_s3.py — boto3 SigV4 pre-signed URLs
                          pointing straight at storage (no /transfer
                          hop in the data path)

All implementations share one interface, so the routes below do not
change between modes. The Lambda handlers in functions/handlers.py run
the same logic with the "s3"/"xsts" choices behind API Gateway.

Routes:
  GET    /health                          liveness + backend disclosure
  POST   /auth/dev-login                  dev login (issues a token for
                                          any gamertag; production has
                                          no login endpoint — clients
                                          arrive with XSTS tokens)
  GET    /saves                           list the caller's saves
  POST   /save            {name}          Phase 5 shape -> {uploadUrl}
  GET    /save?name=...                   Phase 5 shape -> {downloadUrl}
  POST   /saves/{name}/upload-url         REST alias -> {uploadUrl}
  GET    /saves/{name}/download-url       REST alias -> {downloadUrl}
  DELETE /saves/{name}                    delete one of the caller's saves
  PUT    /transfer/{token}                local-presign upload target
  GET    /transfer/{token}                local-presign download target
  GET    /ui                              the frontend (static files)

Errors follow the plan: 400 invalid request, 401 unauthorized,
404 not found, 500 internal — returned as {"error": "..."}.

Run:  uvicorn app:app --port 8000        (from the backend/ directory)

Path-prefix hosting: set BASE_PATH (e.g. "/xbox") to serve the whole
app under that prefix. The gateway in front of the container forwards
requests WITH the prefix; when BASE_PATH is set, a middleware records
it as the ASGI scope's root_path, which drives both route matching
and URL generation (see the bottom of this file). With BASE_PATH
unset the app serves at the root, as in local dev.
"""

import os

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth_stub
import auth_xsts
import presign_s3
import presign_stub
import storage_s3
import storage_stub

# ------------------------------------------------------------ backend selection

AUTH_MODE = os.environ.get("XSAVE_AUTH", "xsts")
STORAGE_MODE = os.environ.get("XSAVE_STORAGE", "local")
PRESIGN_MODE = os.environ.get("XSAVE_PRESIGN", "local")

# Public path prefix when hosted behind a prefix-forwarding gateway
# ("" = serve at root). Normalized to "/xbox" form: leading slash, no
# trailing slash.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH

auth = auth_stub if AUTH_MODE == "stub" else auth_xsts
storage = storage_s3 if STORAGE_MODE == "s3" else storage_stub
presign = presign_s3 if PRESIGN_MODE == "s3" else presign_stub

app = FastAPI(title="Xbox Cloud Save Platform (local dev)", version="0.2.0")

# Phase 7 (API Gateway) requires CORS for browser clients; the local app
# mirrors that so the frontend works unchanged.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------------- errors

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message})


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": "internal server error"})


# ---------------------------------------------------------------- auth

def require_xuid(authorization: str | None = Header(default=None)) -> str:
    """Authenticate the caller and return their XUID.

    Applies the plan's Phase 2 flow: parse_xbl_token ->
    validate_xsts_token (including the header/token user-hash binding
    in xsts mode) -> get_xuid.
    """
    try:
        user_hash, token = auth.parse_xbl_token(authorization)
        payload = auth.validate_xsts_token(token, user_hash)
    except auth.AuthError as exc:
        raise ApiError(401, f"unauthorized: {exc.reason}")
    return auth.get_xuid(payload)


class DevLoginRequest(BaseModel):
    gamertag: str


@app.post("/auth/dev-login")
def dev_login(body: DevLoginRequest):
    """DEV ONLY. Issues a token for any gamertag for local development.

    In "xsts" mode the token is a properly structured XSTS JWT signed
    with the local test key; in "stub" mode it is the Increment 1
    unsigned fake. Production clients receive their XSTS token from
    the Xbox client stack after real Microsoft sign-in; no login
    endpoint exists in the production design.
    """
    gamertag = body.gamertag.strip()
    if not gamertag:
        raise ApiError(400, "invalid request: gamertag is required")
    user_hash, token, exp = auth.issue_dev_token(gamertag)
    payload = auth.validate_xsts_token(token, user_hash)
    return {
        "authorization": f"XBL3.0 x={user_hash};{token}",
        "xuid": auth.get_xuid(payload),
        "gamertag": gamertag,
        "expiresAt": exp,
        "authMode": AUTH_MODE,
        "stub": AUTH_MODE == "stub",
    }


# ---------------------------------------------------------------- saves

@app.get("/health")
def health():
    if AUTH_MODE == "stub":
        auth_desc = "stub — increment 1 unsigned fake tokens"
    else:
        auth_desc = "xsts structure — JWT signature vs local test key (not Microsoft)"
    if STORAGE_MODE == "s3":
        storage_desc = (
            f"boto3 S3 — bucket {storage_s3.get_bucket()}, "
            f"endpoint {storage_s3.ENDPOINT_URL or 'AWS default'}"
        )
    else:
        storage_desc = "stub — local filesystem instead of S3"
    if PRESIGN_MODE == "s3":
        presign_desc = "boto3 SigV4 pre-signed URLs to storage"
    else:
        presign_desc = "stub — HMAC capability tokens instead of SigV4"
    return {
        "status": "ok",
        "service": "xsave-local",
        "basePath": BASE_PATH,
        "modes": {"auth": AUTH_MODE, "storage": STORAGE_MODE, "presign": PRESIGN_MODE},
        "stubs": {
            "xstsAuth": auth_desc,
            "storage": storage_desc,
            "presignedUrls": presign_desc,
        },
    }


@app.get("/saves")
def list_saves(xuid: str = Depends(require_xuid)):
    return {"xuid": xuid, "saves": storage.list_saves(xuid)}


def _upload_url(request: Request, xuid: str, name: str) -> dict:
    try:
        storage.validate_save_name(name)
    except storage.InvalidSaveName as exc:
        raise ApiError(400, f"invalid request: {exc}")
    key = storage.key_for(xuid, name)
    base = str(request.base_url).rstrip("/")
    return {
        "uploadUrl": presign.make_transfer_url(base, "put", key),
        "expiresIn": presign.EXPIRES_IN,
    }


def _download_url(request: Request, xuid: str, name: str) -> dict:
    try:
        storage.validate_save_name(name)
    except storage.InvalidSaveName as exc:
        raise ApiError(400, f"invalid request: {exc}")
    if name not in {s["name"] for s in storage.list_saves(xuid)}:
        raise ApiError(404, "save not found")
    key = storage.key_for(xuid, name)
    base = str(request.base_url).rstrip("/")
    return {
        "downloadUrl": presign.make_transfer_url(base, "get", key),
        "expiresIn": presign.EXPIRES_IN,
    }


class SaveRequest(BaseModel):
    name: str


@app.post("/save")
def request_upload_url(body: SaveRequest, request: Request, xuid: str = Depends(require_xuid)):
    """Phase 5, Endpoint 2: POST /save -> {uploadUrl}."""
    return _upload_url(request, xuid, body.name)


@app.get("/save")
def request_download_url(
    request: Request,
    name: str = Query(...),
    xuid: str = Depends(require_xuid),
):
    """Phase 5, Endpoint 1: GET /save -> {downloadUrl}."""
    return _download_url(request, xuid, name)


@app.post("/saves/{name}/upload-url")
def request_upload_url_rest(name: str, request: Request, xuid: str = Depends(require_xuid)):
    return _upload_url(request, xuid, name)


@app.get("/saves/{name}/download-url")
def request_download_url_rest(name: str, request: Request, xuid: str = Depends(require_xuid)):
    return _download_url(request, xuid, name)


@app.delete("/saves/{name}")
def delete_save(name: str, xuid: str = Depends(require_xuid)):
    try:
        storage.validate_save_name(name)
        meta = storage.delete_save(xuid, name)
    except storage.InvalidSaveName as exc:
        raise ApiError(400, f"invalid request: {exc}")
    except storage.SaveNotFound:
        raise ApiError(404, "save not found")
    return {"deleted": True, "name": meta["name"]}


# ------------------------------------------------------- transfer (S3 sim)
# These routes are the local data path for XSAVE_PRESIGN=local only:
# the HMAC token in the URL is the authorization, exactly as a SigV4
# pre-signed URL is when it points at S3. In "s3" presign mode the
# URLs produced by this API point at storage directly and nothing
# calls these routes.

@app.put("/transfer/{token}")
async def transfer_upload(token: str, request: Request):
    """The local stand-in for 'PUT to a pre-signed S3 URL'."""
    try:
        key = presign_stub.verify_transfer_token(token, "put")
    except presign_stub.PresignError as exc:
        raise ApiError(401, f"unauthorized: {exc.reason}")
    data = await request.body()
    meta = storage.put_bytes(key, data)
    return {"stored": True, **meta}


@app.get("/transfer/{token}")
def transfer_download(token: str):
    """The local stand-in for 'GET from a pre-signed S3 URL'."""
    try:
        key = presign_stub.verify_transfer_token(token, "get")
    except presign_stub.PresignError as exc:
        raise ApiError(401, f"unauthorized: {exc.reason}")
    try:
        data = storage.get_bytes(key)
    except storage.SaveNotFound:
        raise ApiError(404, "save not found")
    return Response(content=data, media_type="application/octet-stream")


# ---------------------------------------------------------------- frontend

_FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "frontend"
)
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/ui", StaticFiles(directory=_FRONTEND_DIR, html=True), name="ui")


# ------------------------------------------------------- prefix hosting
# The deployment gateway forwards requests WITH the public prefix
# (e.g. /xbox/health). Starlette's own contract for this is the ASGI
# scope's root_path: routing and StaticFiles derive each route path
# from scope["path"] minus root_path, and request.base_url (which the
# pre-signed transfer URLs are built from) is built from
# app_root_path. So when BASE_PATH is set, this middleware only
# records the prefix in those two scope keys — it does NOT rewrite
# scope["path"] — and 404s any HTTP request outside the prefix.
# With BASE_PATH unset no middleware is added (local dev, test suite).

class BasePathMiddleware:
    """Record BASE_PATH as the scope root_path; 404 outside the prefix."""

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope = dict(scope)
                scope["root_path"] = self.prefix + scope.get("root_path", "")
                scope["app_root_path"] = self.prefix
                await self.app(scope, receive, send)
                return
            if scope["type"] == "http":
                response = JSONResponse(status_code=404, content={"detail": "Not Found"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


if BASE_PATH:
    app.add_middleware(BasePathMiddleware, prefix=BASE_PATH)
