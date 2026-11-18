#!/usr/bin/env python3
"""
s3_file_cipher.py -- Chunked AES-256-GCM streaming file cipher.

Built for the Cell Tower LAN fleet service from the design in
Cell_Tower_LAN_1zilTB.docx (Taylor, 2026-11-18). Local build + test only:
this utility is not wired into the running Node service, no Lambda is
deployed, and no IAM role is attached anywhere by this file.

Design (as specified in the task / document):
  - AES-256-GCM authenticated encryption (confidentiality + integrity).
  - Password key derivation: PBKDF2-HMAC-SHA256, 600,000 iterations,
    random 16-byte salt per file, 32-byte (256-bit) derived key.
  - Chunked streaming: plaintext is processed in 1 MiB chunks, so files
    far larger than RAM can be encrypted/decrypted. Each chunk is
    encrypted independently with AES-GCM (ciphertext + 16-byte tag).
  - Per-chunk nonce: 12 bytes constructed from
        8-byte big-endian chunk counter
      + 1-byte last-chunk flag (0x01 for the final chunk, else 0x00)
      + first 3 bytes of a random 12-byte per-file base nonce.
    The counter makes every chunk nonce unique; the last-chunk flag in
    the nonce (and in the associated data) binds the final chunk to its
    position, so truncation or chunk reordering fails authentication.
  - Associated data for every chunk: the full file header plus the
    chunk counter and last-chunk flag, so header tampering also fails.
  - Input and output may each be a local path or an s3://bucket/key
    path. S3 reads are streamed from the object body; S3 writes are
    staged through a temporary local file and uploaded only after the
    whole transform succeeds (a failed decrypt never uploads a partial
    result). boto3 is imported lazily, only when an s3:// path is used.
  - Passwords are never written to any file or log. The password is
    read with getpass (no echo), or from an environment variable named
    with --password-env (useful for scripted tests). Passwords are
    never accepted as a plain command-line argument value.

File format (all integers big-endian):
  offset  size  field
  0       8     magic  b"CTLZIL01"
  8       16    PBKDF2 salt
  24      12    base nonce
  36      4     plaintext chunk size (uint32, 1048576 in files we write)
  40      ...   encrypted chunks, each: ciphertext (<= chunk size) + 16-byte GCM tag
                An empty plaintext produces exactly one 0-byte chunk
                (16 bytes on disk: tag only), so its password is still
                authenticated on decrypt.

Usage:
  python3 s3_file_cipher.py encrypt -i plain.bin  -o plain.bin.enc
  python3 s3_file_cipher.py decrypt -i plain.bin.enc -o plain.bin
  python3 s3_file_cipher.py encrypt -i local.bin -o s3://bucket/key.enc
  python3 s3_file_cipher.py decrypt -i s3://bucket/key.enc -o local.bin

Requires: the `cryptography` library; `boto3` only for s3:// paths.
Exit codes: 0 success, 1 any failure (wrong password, tampered data,
missing input, S3 error). On decrypt failure a local output file is
removed so no unauthenticated plaintext is left behind.
"""

import argparse
import os
import sys
import tempfile
from getpass import getpass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"CTLZIL01"
SALT_SIZE = 16
BASE_NONCE_SIZE = 12
KEY_SIZE = 32
ITERATIONS = 600_000
CHUNK_SIZE = 1024 * 1024  # 1 MiB plaintext per chunk
TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + SALT_SIZE + BASE_NONCE_SIZE + 4
DEFAULT_PASSWORD_ENV = "S3_FILE_CIPHER_PASSWORD"


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit key from a password and salt using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def construct_chunk_nonce(base_nonce: bytes, counter: int, is_last: bool) -> bytes:
    """
    Construct a unique 12-byte nonce for one chunk:
      8 bytes: 64-bit chunk counter (big-endian)
      1 byte : last-chunk flag (0x01 if last, 0x00 otherwise)
      3 bytes: base-nonce prefix
    """
    counter_bytes = counter.to_bytes(8, byteorder="big")
    flag_byte = b"\x01" if is_last else b"\x00"
    return counter_bytes + flag_byte + base_nonce[:3]


def chunk_aad(header: bytes, counter: int, is_last: bool) -> bytes:
    return header + counter.to_bytes(8, byteorder="big") + (b"\x01" if is_last else b"\x00")


def _read_exact(stream, n: int) -> bytes:
    data = b""
    while len(data) < n:
        part = stream.read(n - len(data))
        if not part:
            break
        data += part
    return data


# --------------------------------------------------------------------------
# Path handling: local files and s3://bucket/key
# --------------------------------------------------------------------------

def parse_s3_path(path: str):
    """Return (bucket, key) for s3://bucket/key, else None."""
    if not path.startswith("s3://"):
        return None
    rest = path[5:]
    bucket, sep, key = rest.partition("/")
    if not bucket or not sep or not key:
        raise ValueError(f"Invalid S3 path (expected s3://bucket/key): {path}")
    return bucket, key


def open_input(path: str):
    """Open a readable binary stream for a local or s3:// path."""
    s3 = parse_s3_path(path)
    if s3 is None:
        return open(path, "rb")
    import boto3  # lazy: only needed for S3 paths

    bucket, key = s3
    client = boto3.client("s3")
    response = client.get_object(Bucket=bucket, Key=key)
    return response["Body"]


class OutputTarget:
    """
    Writable binary target for a local or s3:// path.
    Local: writes go straight to the file.
    S3: writes are staged in a temporary file; commit() uploads it with
    boto3 only after the transform has succeeded. abort() discards it.
    """

    def __init__(self, path: str):
        self.path = path
        self.s3 = parse_s3_path(path)
        self._tmp = None
        if self.s3 is None:
            self._fh = open(path, "wb")
        else:
            self._tmp = tempfile.NamedTemporaryFile(
                mode="wb", delete=False, prefix="s3_file_cipher_", suffix=".tmp"
            )
            self._fh = self._tmp

    def write(self, data: bytes) -> int:
        return self._fh.write(data)

    def commit(self) -> None:
        self._fh.close()
        if self.s3 is not None:
            import boto3  # lazy: only needed for S3 paths

            bucket, key = self.s3
            client = boto3.client("s3")
            with open(self._tmp.name, "rb") as staged:
                client.upload_fileobj(staged, bucket, key)
            os.unlink(self._tmp.name)
            self._tmp = None

    def abort(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
        if self.s3 is None:
            # Never leave unauthenticated / partial plaintext behind.
            try:
                os.unlink(self.path)
            except OSError:
                pass
        elif self._tmp is not None:
            try:
                os.unlink(self._tmp.name)
            except OSError:
                pass
            self._tmp = None


# --------------------------------------------------------------------------
# Core streaming transforms (operate on binary streams)
# --------------------------------------------------------------------------

def encrypt_stream(in_stream, out_target: OutputTarget, password: str) -> None:
    salt = os.urandom(SALT_SIZE)
    base_nonce = os.urandom(BASE_NONCE_SIZE)
    header = MAGIC + salt + base_nonce + CHUNK_SIZE.to_bytes(4, byteorder="big")
    key = derive_key(password, salt)
    aesgcm = AESGCM(key)

    out_target.write(header)

    counter = 0
    current = in_stream.read(CHUNK_SIZE)
    if current == b"":
        # Empty input: one final zero-length chunk authenticates the password.
        nonce = construct_chunk_nonce(base_nonce, 0, True)
        out_target.write(aesgcm.encrypt(nonce, b"", chunk_aad(header, 0, True)))
        return

    while True:
        following = in_stream.read(CHUNK_SIZE)
        is_last = following == b""
        nonce = construct_chunk_nonce(base_nonce, counter, is_last)
        out_target.write(aesgcm.encrypt(nonce, current, chunk_aad(header, counter, is_last)))
        if is_last:
            break
        current = following
        counter += 1


def decrypt_stream(in_stream, out_target: OutputTarget, password: str) -> None:
    header = _read_exact(in_stream, HEADER_SIZE)
    if len(header) < HEADER_SIZE or not header.startswith(MAGIC):
        raise ValueError("Not an s3_file_cipher file (bad or truncated header).")

    salt = header[len(MAGIC):len(MAGIC) + SALT_SIZE]
    base_nonce = header[len(MAGIC) + SALT_SIZE:len(MAGIC) + SALT_SIZE + BASE_NONCE_SIZE]
    chunk_size = int.from_bytes(header[-4:], byteorder="big")
    if chunk_size <= 0 or chunk_size > 64 * 1024 * 1024:
        raise ValueError(f"Unreasonable chunk size in header: {chunk_size}")
    enc_chunk_size = chunk_size + TAG_SIZE

    key = derive_key(password, salt)
    aesgcm = AESGCM(key)

    counter = 0
    current = _read_exact(in_stream, enc_chunk_size)
    if current == b"":
        raise ValueError("Truncated file: header present but no encrypted chunks.")

    while True:
        following = _read_exact(in_stream, enc_chunk_size)
        is_last = following == b""
        nonce = construct_chunk_nonce(base_nonce, counter, is_last)
        try:
            plaintext = aesgcm.decrypt(nonce, current, chunk_aad(header, counter, is_last))
        except InvalidTag:
            raise ValueError(
                "Decryption failed: incorrect password or corrupted/tampered data."
            )
        out_target.write(plaintext)
        if is_last:
            break
        current = following
        counter += 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def get_password(args, confirm: bool) -> str:
    env_name = args.password_env or DEFAULT_PASSWORD_ENV
    password = os.environ.get(env_name)
    if password:
        return password
    password = getpass("Enter password: ")
    if not password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        sys.exit(1)
    if confirm:
        confirm_password = getpass("Confirm password: ")
        if password != confirm_password:
            print("Error: Passwords do not match.", file=sys.stderr)
            sys.exit(1)
    return password


def run(mode: str, input_path: str, output_path: str, password: str) -> None:
    with open_input(input_path) as in_stream:
        out = OutputTarget(output_path)
        try:
            if mode == "encrypt":
                encrypt_stream(in_stream, out, password)
            else:
                decrypt_stream(in_stream, out, password)
        except Exception:
            out.abort()
            raise
        out.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chunked AES-256-GCM file cipher with local and s3:// paths"
    )
    parser.add_argument(
        "--password-env",
        default=None,
        help=(
            "Name of an environment variable holding the password "
            f"(default: {DEFAULT_PASSWORD_ENV} if set, otherwise prompt with getpass)"
        ),
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    enc = subparsers.add_parser("encrypt", help="Encrypt a file")
    enc.add_argument("-i", "--input", required=True, help="Input path (local or s3://bucket/key)")
    enc.add_argument("-o", "--output", required=True, help="Output path (local or s3://bucket/key)")

    dec = subparsers.add_parser("decrypt", help="Decrypt a file")
    dec.add_argument("-i", "--input", required=True, help="Input path (local or s3://bucket/key)")
    dec.add_argument("-o", "--output", required=True, help="Output path (local or s3://bucket/key)")

    args = parser.parse_args()
    password = get_password(args, confirm=(args.mode == "encrypt"))

    try:
        run(args.mode, args.input, args.output, password)
    except FileNotFoundError as exc:
        print(f"Error: File not found: {exc.filename}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # S3 / boto3 and other I/O errors surface in full
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"OK {args.mode} -> '{args.output}'")


if __name__ == "__main__":
    main()
