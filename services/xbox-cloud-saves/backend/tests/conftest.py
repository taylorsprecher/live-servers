"""Shared fixtures for the Xbox Cloud Save test suite.

The API tests run the FastAPI app in its default local modes
(XSAVE_AUTH=xsts signed tokens, local filesystem storage, local HMAC
presign) with storage redirected to a tmp dir. The S3/Lambda tests run
the boto3 layers against moto's in-process S3 — no AWS, no network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    """Point the local-filesystem storage stub at a fresh tmp dir."""
    import storage_stub

    monkeypatch.setattr(storage_stub, "STORAGE_DIR", str(tmp_path / "storage_data"))
    return tmp_path


@pytest.fixture()
def api_client(local_storage):
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as client:
        yield client


@pytest.fixture()
def login(api_client):
    """Return a helper that dev-logs in and yields the login JSON."""
    def _login(gamertag: str) -> dict:
        resp = api_client.post("/auth/dev-login", json={"gamertag": gamertag})
        assert resp.status_code == 200, resp.text
        return resp.json()

    return _login


@pytest.fixture()
def auth_header(login):
    """Return a helper producing an Authorization header per gamertag."""
    def _header(gamertag: str) -> dict:
        return {"Authorization": login(gamertag)["authorization"]}

    return _header


@pytest.fixture()
def s3():
    """The boto3 storage layer against a fresh moto S3 + bucket."""
    from moto import mock_aws

    import storage_s3

    with mock_aws():
        storage_s3.reset_client()
        storage_s3.ensure_bucket()
        yield storage_s3
        storage_s3.reset_client()
