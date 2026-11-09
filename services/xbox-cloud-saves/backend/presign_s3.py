"""
Pre-signed URL service — boto3 SigV4 implementation (Increment 2).

Production Phase 6: clients never touch S3 directly; the API returns a
boto3 generate_presigned_url() for exactly one object and one
operation, expiring after 15 minutes (ExpiresIn=900).

This module shares the interface the FastAPI app uses
(make_transfer_url / EXPIRES_IN / PresignError) but the returned URL
points at S3 itself (or LocalStack / moto when XSAVE_S3_ENDPOINT is
set), not at the API — in this mode there is no /transfer endpoint in
the data path at all.

verify_transfer_token() exists only for interface parity: a real SigV4
pre-signed URL is verified by S3, not by this service, so calling it
here is a programming error and raises PresignError.
"""

import storage_s3

EXPIRES_IN = 900  # 15 minutes, per Phase 6 of the plan

_OP_TO_METHOD = {"put": "put_object", "get": "get_object"}


class PresignError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def make_transfer_url(base_url: str, op: str, key: str) -> str:
    """Return a SigV4 pre-signed URL for one operation on one key.

    `base_url` (the API's own base URL) is accepted for interface
    parity with presign_stub and ignored: S3 pre-signed URLs are
    addressed to the storage endpoint, not the API.
    """
    if op not in _OP_TO_METHOD:
        raise PresignError(f"unsupported operation: {op!r}")
    return storage_s3.get_client().generate_presigned_url(
        _OP_TO_METHOD[op],
        Params={"Bucket": storage_s3.get_bucket(), "Key": key},
        ExpiresIn=EXPIRES_IN,
    )


def verify_transfer_token(token: str, expected_op: str) -> str:
    raise PresignError(
        "SigV4 pre-signed URLs are verified by S3, not by the API; "
        "there is no /transfer token to verify in s3 presign mode"
    )
