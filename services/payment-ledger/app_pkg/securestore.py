import os
import json
import uuid
import shutil
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from .appdirs import user_data_dir
from cryptography.fernet import Fernet, InvalidToken

APP_NAME = "MyApp"
MAX_SNAPSHOTS = 5


class FileLock:
    """Provides simple filesystem lock safety per target file."""
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def acquire(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            raise TimeoutError(f"Could not acquire lock for {self.lock_path.name}")

    def release(self):
        self.lock_path.unlink(missing_ok=True)


class SecureStore:
    """
    A transactional, crash-safe, encrypted storage engine designed for iOS/Juno.
    Supports multi-file transactions, SHA-256 sidecar checksums, and recovery.
    """
    def __init__(self, key: bytes, app_name: str = APP_NAME):
        self.cipher = Fernet(key)
        self.data_dir = Path(user_data_dir(app_name))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.backup_dir = self.data_dir / "backups"
        self.backup_dir.mkdir(exist_ok=True)

        self.quarantine_dir = self.data_dir / "quarantine"
        self.quarantine_dir.mkdir(exist_ok=True)

        self.txn_dir = self.data_dir / "transactions"
        self.txn_dir.mkdir(exist_ok=True)

        self.locks_dir = self.data_dir / "locks"
        self.locks_dir.mkdir(exist_ok=True)

        # Execute transaction recovery at startup
        self._recover_uncommitted_transactions()

    # ------------------------------------------------------------------
    # Encryption & Hashing Helpers
    # ------------------------------------------------------------------
    def _encrypt(self, data: Dict[str, Any]) -> bytes:
        plaintext = json.dumps(data, indent=2).encode("utf-8")
        return self.cipher.encrypt(plaintext)

    def _decrypt(self, encrypted: bytes) -> Dict[str, Any]:
        plaintext = self.cipher.decrypt(encrypted)
        return json.loads(plaintext.decode("utf-8"))

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ------------------------------------------------------------------
    # Low-level Atomic IO operations
    # ------------------------------------------------------------------
    def _atomic_write_bytes(self, target_path: Path, data: bytes):
        with tempfile.NamedTemporaryFile(mode="wb", dir=self.data_dir, delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.replace(temp_name, target_path)

    def _atomic_write_json(self, target_path: Path, data: Dict[str, Any]):
        json_bytes = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write_bytes(target_path, json_bytes)

    # ------------------------------------------------------------------
    # Checksum & Quarantine Utilities
    # ------------------------------------------------------------------
    def _write_checksum(self, filename: str, checksum: str):
        checksum_file = self.data_dir / f"{filename}.sha256"
        self._atomic_write_bytes(checksum_file, checksum.encode("utf-8"))

    def _verify_checksum(self, target: Path, encrypted_bytes: bytes) -> bool:
        checksum_file = self.data_dir / f"{target.name}.sha256"
        if not checksum_file.exists():
            return True  # Fall back to Fernet's intrinsic authentication if absent
        expected = checksum_file.read_text(encoding="utf-8").strip()
        actual = self._sha256_bytes(encrypted_bytes)
        return actual == expected

    def _quarantine_file(self, path: Path):
        if not path.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rand_id = uuid.uuid4().hex[:6]
        quarantined = self.quarantine_dir / f"{path.name}.{stamp}.{rand_id}.corrupt"
        try:
            shutil.move(path, quarantined)
        except Exception:
            pass  # Non-fatal error; failure to quarantine shouldn't halt recovery

    # ------------------------------------------------------------------
    # Backups & Snapshots
    # ------------------------------------------------------------------
    def _create_backups_and_snapshots(self, filename: str):
        target = self.data_dir / filename
        backup = self.data_dir / f"{filename}.bak"

        if target.exists():
            shutil.copy2(target, backup)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot = self.backup_dir / f"{filename}.{stamp}"
            shutil.copy2(target, snapshot)

            # Copy matching sidecar checksums if available
            checksum_src = self.data_dir / f"{filename}.sha256"
            if checksum_src.exists():
                shutil.copy2(checksum_src, self.backup_dir / f"{filename}.{stamp}.sha256")

            self._cleanup_old_snapshots(filename)

    def _cleanup_old_snapshots(self, filename: str):
        snapshots = sorted(
            self.backup_dir.glob(f"{filename}.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        # Exclude sidecar .sha256 files from rotation counts
        data_snapshots = [s for s in snapshots if not s.name.endswith(".sha256")]
        for old in data_snapshots[MAX_SNAPSHOTS:]:
            old.unlink(missing_ok=True)
            sha_sidecar = self.backup_dir / f"{old.name}.sha256"
            sha_sidecar.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Public API: Single-file Operations
    # ------------------------------------------------------------------
    def save(self, filename: str, data: Dict[str, Any]) -> Path:
        """Saves a single file within a 2PC atomic transaction context."""
        return self.transaction({filename: data})[filename]

    def load(self, filename: str) -> Dict[str, Any]:
        """
        Loads data using a 3-tier recovery approach:
        Tier 1: Primary file
        Tier 2: Backup file (.bak)
        Tier 3: Historical Snapshots
        """
        target = self.data_dir / filename
        backup = self.data_dir / f"{filename}.bak"

        # Tier 1: Try Primary File
        if target.exists():
            try:
                data_bytes = target.read_bytes()
                if self._verify_checksum(target, data_bytes):
                    return self._decrypt(data_bytes)
                else:
                    self._quarantine_file(target)
            except (InvalidToken, json.JSONDecodeError, Exception):
                self._quarantine_file(target)

        # Tier 2: Try Backup
        if backup.exists():
            try:
                data_bytes = backup.read_bytes()
                data = self._decrypt(data_bytes)
                self._atomic_write_bytes(target, data_bytes)  # Repair primary
                return data
            except Exception:
                self._quarantine_file(backup)

        # Tier 3: Try Historical Snapshots
        snapshots = sorted(
            [s for s in self.backup_dir.glob(f"{filename}.*") if not s.name.endswith(".sha256")],
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for snapshot in snapshots:
            try:
                data_bytes = snapshot.read_bytes()
                data = self._decrypt(data_bytes)
                self._atomic_write_bytes(target, data_bytes)  # Repair primary
                return data
            except Exception:
                self._quarantine_file(snapshot)

        return {}

    # ------------------------------------------------------------------
    # Transactional Multi-File Two-Phase Commit Engine
    # ------------------------------------------------------------------
    def transaction(self, payload: Dict[str, Dict[str, Any]]) -> Dict[str, Path]:
        """
        Executes a multi-file atomic write using Two-Phase Commit (2PC):
        1. Acquires lock for all files
        2. Creates backups and historical snapshots
        3. Writes transaction journal (PREPARED state)
        4. Writes temporary files & syncs to disk (Phase 1)
        5. Commits files & updates checksums (Phase 2)
        6. Clears transaction record and unlocks
        """
        txid = f"txn_{uuid.uuid4().hex[:8]}"
        journal_path = self.txn_dir / f"{txid}.json"
        locks = [FileLock(self.locks_dir / f"{fname}.lock") for fname in payload.keys()]

        # Acquire all locks
        for lock in locks:
            lock.acquire()

        try:
            prepared_files = {}
            # Phase 1: Preparation & Encryption
            for filename, content in payload.items():
                self._create_backups_and_snapshots(filename)
                encrypted = self._encrypt(content)
                checksum = self._sha256_bytes(encrypted)

                # Write payload temp file
                tmp_target = self.data_dir / f"{filename}.tmp"
                self._atomic_write_bytes(tmp_target, encrypted)

                prepared_files[filename] = {
                    "checksum": checksum,
                    "tmp_path": str(tmp_target)
                }

            # Record Journal: PREPARED
            journal = {
                "id": txid,
                "state": "PREPARED",
                "files": prepared_files
            }
            self._atomic_write_json(journal_path, journal)

            # Phase 2: Commit
            journal["state"] = "COMMITTED"
            self._atomic_write_json(journal_path, journal)

            results = {}
            for filename, meta in prepared_files.items():
                target = self.data_dir / filename
                tmp_target = Path(meta["tmp_path"])

                os.replace(tmp_target, target)
                self._write_checksum(filename, meta["checksum"])
                results[filename] = target

            # Transaction finished successfully
            journal_path.unlink(missing_ok=True)
            return results

        finally:
            for lock in locks:
                lock.release()

    def _recover_uncommitted_transactions(self):
        """Startup Recovery: Examines journal logs and repairs broken state."""
        for journal_file in self.txn_dir.glob("*.json"):
            try:
                journal = json.loads(journal_file.read_text(encoding="utf-8"))
                state = journal.get("state")
                files = journal.get("files", {})

                if state == "PREPARED":
                    # Abort: discard temporary files
                    for filename, meta in files.items():
                        Path(meta["tmp_path"]).unlink(missing_ok=True)

                elif state == "COMMITTED":
                    # Complete roll-forward
                    for filename, meta in files.items():
                        tmp_target = Path(meta["tmp_path"])
                        target = self.data_dir / filename
                        if tmp_target.exists():
                            os.replace(tmp_target, target)
                            self._write_checksum(filename, meta["checksum"])

                journal_file.unlink(missing_ok=True)
            except Exception:
                pass


# ------------------------------------------------------------------
# Key Setup & Run Example
# ------------------------------------------------------------------
if __name__ == "__main__":
    # Generate key once
    key = Fernet.generate_key()

    # Initialize store
    store = SecureStore(key=key)

    # 1. Multi-file Transactional Save
    store.transaction({
        "settings.enc": {"theme": "dark", "font_size": 14},
        "tokens.enc": {"access_token": "secret_abc123"}
    })

    # 2. Reading files
    print("Settings:", store.load("settings.enc"))
    print("Tokens:", store.load("tokens.enc"))

    # 3. Simulating file corruption & auto-recovery test
    target_file = store.data_dir / "settings.enc"
    target_file.write_bytes(b"CORRUPTED_GARBAGE")

    print("\n[Corruption Test] Primary file modified with invalid payload.")
    recovered_settings = store.load("settings.enc")
    print("Recovered Result:", recovered_settings)
