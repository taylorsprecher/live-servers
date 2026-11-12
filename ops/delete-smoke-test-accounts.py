#!/usr/bin/env python3
"""One-off: delete the two throwaway Stripe smoke-test member accounts.

Runs INSIDE the live-members container (via ops/delete-smoke-test-
accounts.sh + the delete-smoke-test-accounts GitHub workflow), against
the member site's SQLite DB at MEMBERS_DB_PATH (/data/members.db).

Background: during the 2026-11-12 Stripe go-live attempt, two roster
accounts were created purely as checkout smoke tests. Their checkouts
failed at session creation, they never paid, and the Stripe go-live was
closed the same day (Taylor: "Let's just keep Cash App"). Taylor
approved their deletion ("Delete both smoke-test accounts for me",
2026-11-12). The roster UI offers grant/revoke only, so the accounts
are removed here, server-side, in a single transaction.

Deletes, for the two target emails ONLY: access (entitlements),
sessions, payments, manual_orders, member_uploads, any uploads rows
keyed by those emails, and finally the member rows — the same table
set the app's own member-removal code path uses.

Guard rails:
  - OWNER PROTECTION: if either target ever equals the owner email,
    abort before touching anything.
  - STATE CHECK: abort if total members BEFORE is not exactly 3, or
    if the owner row is missing — unexpected state is reported, never
    improvised around.
  - SURVIVOR CHECK: after the delete, the sole remaining member must
    be the owner, and every per-table target count must be 0.
  - Password hashes/salts are never selected or printed; only counts,
    member ids, and the target emails themselves appear in the report.
"""
import os
import sqlite3
import sys

DB_PATH = os.environ.get("MEMBERS_DB_PATH", "/data/members.db")
OWNER_EMAIL = "taylorsprecher@gmail.com"
TARGET_EMAILS = [
    "stripe-smoke-test-20261112@example.com",
    "stripe-smoke-test2-20261112@example.com",
]

# Owner protection, checked before any DB work.
for target in TARGET_EMAILS:
    if target.strip().lower() == OWNER_EMAIL.lower():
        print(f"ABORT: target {target!r} matches the owner account — refusing to run.")
        sys.exit(2)


def target_ids(db):
    ids = []
    for email in TARGET_EMAILS:
        row = db.execute(
            "SELECT id FROM members WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
        ids.append(row[0] if row else None)
    return ids


def counts(db, ids):
    """(total_members, {table: rows belonging to the targets})."""
    total = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    present_ids = [i for i in ids if i is not None]
    per_table = {}
    per_table["members"] = db.execute(
        "SELECT COUNT(*) FROM members WHERE email IN (%s)"
        % ",".join("?" for _ in TARGET_EMAILS),
        TARGET_EMAILS,
    ).fetchone()[0]
    for table in ("access", "sessions", "payments", "manual_orders", "member_uploads"):
        if present_ids:
            per_table[table] = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE member_id IN (%s)"
                % ",".join("?" for _ in present_ids),
                present_ids,
            ).fetchone()[0]
        else:
            per_table[table] = 0
    per_table["uploads_by_email"] = db.execute(
        "SELECT COUNT(*) FROM uploads WHERE owner_email IN (%s)"
        % ",".join("?" for _ in TARGET_EMAILS),
        TARGET_EMAILS,
    ).fetchone()[0]
    return total, per_table


def report(label, total, per_table, ids):
    print(f"--- {label} ---")
    print(f"total members: {total}")
    for email, mid in zip(TARGET_EMAILS, ids):
        print(f"target {email}: member_id={mid if mid is not None else 'NOT FOUND'}")
    for table, n in per_table.items():
        print(f"target rows in {table}: {n}")


def main():
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    db.execute("PRAGMA busy_timeout = 30000")
    try:
        ids = target_ids(db)
        total_before, before = counts(db, ids)
        report("BEFORE", total_before, before, ids)

        if total_before != 3:
            print(
                f"ABORT: expected exactly 3 members before deletion, found "
                f"{total_before}. No changes made."
            )
            return 1
        owner_row = db.execute(
            "SELECT id FROM members WHERE email = ? COLLATE NOCASE", (OWNER_EMAIL,)
        ).fetchone()
        if owner_row is None:
            print("ABORT: owner account row not found. No changes made.")
            return 1
        if owner_row[0] in [i for i in ids if i is not None]:
            print("ABORT: a target id resolves to the owner account. No changes made.")
            return 2

        present_ids = [i for i in ids if i is not None]
        db.execute("BEGIN IMMEDIATE")
        try:
            # Re-verify inside the transaction.
            total_now = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
            if total_now != 3:
                db.execute("ROLLBACK")
                print(
                    f"ABORT: member count changed during run ({total_now}). "
                    "Rolled back, no changes made."
                )
                return 1
            if present_ids:
                ph = ",".join("?" for _ in present_ids)
                for table in (
                    "access",
                    "sessions",
                    "payments",
                    "manual_orders",
                    "member_uploads",
                ):
                    db.execute(
                        f"DELETE FROM {table} WHERE member_id IN ({ph})", present_ids
                    )
            db.execute(
                "DELETE FROM uploads WHERE owner_email IN (%s)"
                % ",".join("?" for _ in TARGET_EMAILS),
                TARGET_EMAILS,
            )
            if present_ids:
                db.execute(
                    "DELETE FROM members WHERE id IN (%s)"
                    % ",".join("?" for _ in present_ids),
                    present_ids,
                )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

        ids_after = target_ids(db)
        total_after, after = counts(db, ids_after)
        report("AFTER", total_after, after, ids_after)

        survivor = db.execute("SELECT email FROM members").fetchall()
        survivor_ok = (
            total_after == 1
            and len(survivor) == 1
            and survivor[0][0].lower() == OWNER_EMAIL.lower()
        )
        targets_gone = all(n == 0 for n in after.values())
        print(f"sole survivor is the owner account: {survivor_ok}")
        print(f"all target rows removed: {targets_gone}")
        if not (survivor_ok and targets_gone):
            print("RESULT: UNEXPECTED END STATE — investigate before doing anything else.")
            return 1
        print("RESULT: OK — both smoke-test accounts deleted; owner account untouched.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
