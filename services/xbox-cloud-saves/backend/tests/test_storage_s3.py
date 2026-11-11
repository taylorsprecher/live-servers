"""boto3 storage layer against moto S3: same contract as the local
stub, S3 key layout, metadata model, and cross-user separation
(Phases 3 and 12 against the production-shaped backend)."""

import hashlib

import pytest

ALICE = "2535111111111111"
BOB = "2535222222222222"


def test_key_layout_and_name_rules(s3):
    assert s3.key_for(ALICE, "save_slot1.dat") == f"users/{ALICE}/savefiles/save_slot1.dat"
    for bad in ["../evil", "a/b", "", "..", "bad name!"]:
        with pytest.raises(s3.InvalidSaveName):
            s3.validate_save_name(bad)
    assert s3.validate_save_name("save_slot1.dat") == "save_slot1.dat"


def test_put_get_round_trip_and_versioning(s3):
    key = s3.key_for(ALICE, "profile.dat")
    meta1 = s3.put_bytes(key, b"first")
    assert meta1["saveVersion"] == 1
    assert meta1["size"] == 5
    assert meta1["sha256"] == hashlib.sha256(b"first").hexdigest()
    assert s3.get_bytes(key) == b"first"

    meta2 = s3.put_bytes(key, b"second-version")
    assert meta2["saveVersion"] == 2
    assert s3.get_bytes(key) == b"second-version"

    listing = s3.list_saves(ALICE)
    assert len(listing) == 1 and listing[0]["saveVersion"] == 2


def test_objects_land_under_user_prefix(s3):
    s3.put_bytes(s3.key_for(ALICE, "a.dat"), b"a")
    s3.put_bytes(s3.key_for(BOB, "b.dat"), b"b")
    keys = {
        obj["Key"]
        for obj in s3.get_client().list_objects_v2(Bucket=s3.get_bucket()).get("Contents", [])
    }
    assert f"users/{ALICE}/savefiles/a.dat" in keys
    assert f"users/{ALICE}/_index.json" in keys
    assert f"users/{BOB}/savefiles/b.dat" in keys


def test_user_isolation(s3):
    s3.put_bytes(s3.key_for(ALICE, "alice.dat"), b"alice")
    assert s3.list_saves(BOB) == []
    with pytest.raises(s3.SaveNotFound):
        s3.delete_save(BOB, "alice.dat")
    # Alice's save survives Bob's delete attempt.
    assert s3.get_bytes(s3.key_for(ALICE, "alice.dat")) == b"alice"


def test_get_missing_and_delete_missing(s3):
    with pytest.raises(s3.SaveNotFound):
        s3.get_bytes(s3.key_for(ALICE, "nope.dat"))
    with pytest.raises(s3.SaveNotFound):
        s3.delete_save(ALICE, "nope.dat")


def test_delete_removes_object_and_index_entry(s3):
    key = s3.key_for(ALICE, "gone.dat")
    s3.put_bytes(key, b"bye")
    meta = s3.delete_save(ALICE, "gone.dat")
    assert meta["name"] == "gone.dat"
    assert s3.list_saves(ALICE) == []
    with pytest.raises(s3.SaveNotFound):
        s3.get_bytes(key)


def test_index_uploaded_object(s3):
    """The production path: bytes land via a raw S3 PUT (pre-signed
    URL), then the ObjectCreated indexer records the metadata."""
    client = s3.get_client()
    key = s3.key_for(ALICE, "direct.dat")
    client.put_object(Bucket=s3.get_bucket(), Key=key, Body=b"direct-bytes")
    assert s3.list_saves(ALICE) == []  # not indexed yet

    meta = s3.index_uploaded_object(key)
    assert meta["saveVersion"] == 1
    assert meta["sha256"] == hashlib.sha256(b"direct-bytes").hexdigest()
    assert [s["name"] for s in s3.list_saves(ALICE)] == ["direct.dat"]

    # The index object itself and non-save keys are ignored.
    assert s3.index_uploaded_object(f"users/{ALICE}/_index.json") is None
    assert s3.index_uploaded_object("somewhere/else.txt") is None
