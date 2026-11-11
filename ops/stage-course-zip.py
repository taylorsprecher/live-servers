#!/usr/bin/env python3
"""Stage the master CCCE course zip as member-site content.

Runs INSIDE the live-members container (stdin-piped by
ops/stage-course-zip.sh), where /data is the members-data volume.

The zip reached the volume through the member-upload inbox of a
throwaway test account (transfer stayed on Taylor's own server — the
plaintext zip was never committed to git and never touched a
third-party store). This script copies it to the two product content
dirs the owner upload endpoint writes to:
    /data/content/ccce/CCCE_Course_Files.zip
    /data/content/emspr/CCCE_Course_Files.zip
and verifies each copy's MD5 against the master on file. It also
grants the throwaway account CCCE access so the entitled download
path can be verified end-to-end; that grant disappears when the
throwaway account is deleted (the delete-account flow removes access
rows, the member's inbox files, and the account itself).
"""
import glob
import hashlib
import os
import shutil
import sqlite3
import sys

EXPECTED_MD5 = "d456b0c6bf3a20718b02a64313d6a224"
MEMBER_EMAIL = "jasmine-test-a@example.org"
DB_PATH = "/data/members.db"


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


db = sqlite3.connect(DB_PATH)
row = db.execute(
    "SELECT id FROM members WHERE email = ?", (MEMBER_EMAIL,)
).fetchone()
if row is None:
    sys.exit(f"ABORT: throwaway member {MEMBER_EMAIL} not found")
member_id = row[0]

candidates = glob.glob(f"/data/uploads/{member_id}/CCCE_Course_Files.zip")
if not candidates:
    sys.exit("ABORT: uploaded zip not found in the member inbox")
src = candidates[0]

src_md5 = md5_of(src)
print(f"source: {src} md5={src_md5}")
if src_md5 != EXPECTED_MD5:
    sys.exit("ABORT: source MD5 does not match the master on file")

for product in ("ccce", "emspr"):
    dest_dir = f"/data/content/{product}"
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "CCCE_Course_Files.zip")
    shutil.copyfile(src, dest)
    placed_md5 = md5_of(dest)
    print(f"placed: {dest} md5={placed_md5}")
    if placed_md5 != EXPECTED_MD5:
        sys.exit(f"ABORT: placed copy for {product} failed MD5 verification")

db.execute(
    "INSERT OR IGNORE INTO access (member_id, course_key) VALUES (?, 'ccce')",
    (member_id,),
)
db.commit()
print(f"test-only grant: member {member_id} -> ccce")
print("STAGE OK")
