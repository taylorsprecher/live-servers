"""
Demo keystore — the local Ed25519 identity for this AirCore build.

Increment 2 generates ONE demo developer identity locally and persists it
under backend/keys/:

    developer.seed   32-byte Ed25519 seed, hex (PRIVATE — demo only)
    developer.pub    32-byte Ed25519 public key, hex

Honesty notes:
  * This is a DEMO identity generated on this machine for this build. It is
    not a production key, not hardware-bound (the spec's hardware root of
    trust remains stubbed via the device token), and the seed file is
    plaintext on local disk. A production build would hold the developer
    seed in an HSM / OS keychain and ship only the public key.
  * load_or_create_keypair() is idempotent: the first run generates the
    identity; later runs load the same one so signatures stay verifiable
    across restarts and packaged Airlets stay loadable.
"""

from __future__ import annotations

import os

from ed25519 import KeyPair

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_DIR = os.path.join(HERE, "keys")
SEED_PATH = os.path.join(KEYS_DIR, "developer.seed")
PUB_PATH = os.path.join(KEYS_DIR, "developer.pub")


def load_or_create_keypair() -> KeyPair:
    """Load the demo developer identity, generating it on first use."""
    if os.path.exists(SEED_PATH):
        with open(SEED_PATH, "r", encoding="ascii") as fh:
            seed = bytes.fromhex(fh.read().strip())
        return KeyPair(seed)
    os.makedirs(KEYS_DIR, exist_ok=True)
    kp = KeyPair.generate()
    with open(SEED_PATH, "w", encoding="ascii") as fh:
        fh.write(kp.seed_hex + "\n")
    with open(PUB_PATH, "w", encoding="ascii") as fh:
        fh.write(kp.public_hex + "\n")
    return kp


def trusted_developer_pub() -> str:
    """Public hex of the registered demo developer key."""
    return load_or_create_keypair().public_hex
