"""
AirShield — zero-trust policy engine.

Follows the spec's AirShield policy-execution pipeline
(BlackBerry LAN.docx, "AirShield — Full Policy Code Map"):

    1. POLICY_LOAD            load policy manifest
    2. VERIFY_SIGNATURE       validate message signature (Ed25519)
    3. VERIFY_NONCE           ensure message is fresh (anti-replay)
    4. CHECK_IDENTITY_BINDING confirm hardware-bound identity
    5. CHECK_DEVELOPER_KEY    presented key == registered developer key
    6. CHECK_AIRLET_SIGNATURE bundle signature verifies over bundle hash
    7. CHECK_PERMISSION       action allowed by policy permissions?
    8. CHECK_SANDBOX          sandbox constraints met?
    9. POLICY_ALLOW / POLICY_DENY

Increment 2 (2026-11-09): signatures are REAL Ed25519 (RFC 8032, see
ed25519.py) using the locally generated demo developer identity
(keys.py). The Increment 1 HMAC-SHA256 stand-in is gone.

What is real here: the pipeline order, Ed25519 signing/verification of
messages and Airlet bundles, the permission lookup against the spec's
policy manifest, the sandbox-limit constants, nonce replay tracking,
bundle hashing (SHA-256, as the spec names), bundle load-time
verification (verify_bundle), and a full decision log.

What remains STUBBED (clearly, deliberately):
  * Hardware identity binding: compared against a configured demo device
    token. There is no actual hardware root of trust.
  * The demo collapses roles: the same locally generated developer
    identity signs messages and Airlet bundles, and AirShield trusts that
    identity's public key. In production the developer seed would never
    be present on the verifying side — only the public key would.
  * Ed25519 here is pure Python (correct, RFC-tested, but not
    constant-time); see ed25519.py.
"""

from __future__ import annotations

import hashlib
import secrets

import ed25519 as ed
from keys import load_or_create_keypair

DEMO_DEVICE_TOKEN = "air-demo-device-001"


# ---------------------------------------------------------------------------
# Minimal parser for the policy.yaml subset used by the spec's manifest
# (two-level mapping, bool/int/string scalars). Not a general YAML parser.
# ---------------------------------------------------------------------------

def _scalar(text: str):
    text = text.strip()
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    try:
        return int(text)
    except ValueError:
        return text.strip("\"'")


def load_policy(path: str) -> dict:
    policy = {}
    section = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if not line.startswith(" "):
                section = line.strip().rstrip(":")
                policy[section] = {}
            else:
                key, _, value = line.strip().partition(":")
                policy[section][key.strip()] = _scalar(value)
    return policy


# Maps an AirBridge/VM action name to the policy permission that gates it.
ACTION_PERMISSIONS = {
    "execute": "aircore_exec",
    "aircore_exec": "aircore_exec",
    "diagnostics": "diagnostics",
    "network": "network",
    "filesystem": "filesystem",
    "airbridge_send": "airbridge_send",
    "airui_render": "airui_render",
    "sandbox_tools": "sandbox_tools",
    "status": "diagnostics",
}


class AirShield:
    """Policy engine. Every evaluate() call appends to `decision_log`."""

    def __init__(self, policy: dict, device_token: str = DEMO_DEVICE_TOKEN,
                 keypair=None, trusted_developer_pub: str | None = None):
        self.policy = policy
        self.device_token = device_token
        # The local demo identity (signs outgoing messages/bundles) and the
        # registered developer public key AirShield trusts. They coincide in
        # this demo build; see the module docstring's honesty notes.
        self.keypair = keypair or load_or_create_keypair()
        self.trusted_developer_pub = (
            trusted_developer_pub or self.keypair.public_hex)
        self._seen_nonces = set()
        self.decision_log = []

    # -- signing (REAL Ed25519, RFC 8032) ------------------------------------
    def sign(self, body: str) -> str:
        """Sign a string body with the local identity; hex signature."""
        return ed.sign_hex(self.keypair, body)

    @staticmethod
    def verify(body: str, signature_hex: str, public_hex: str) -> bool:
        """Verify an Ed25519 hex signature over a string body."""
        return ed.verify_hex(public_hex, body, signature_hex)

    @staticmethod
    def new_nonce() -> str:
        return secrets.token_hex(8)

    def hash_bundle(self, bundle_bytes: bytes) -> str:
        """HASH_BUNDLE — SHA-256 bundle hashing (real, per spec)."""
        return hashlib.sha256(bundle_bytes).hexdigest()

    # -- individual pipeline stages -----------------------------------------
    def _verify_signature(self, message: dict) -> bool:
        # Verify under the public key PRESENTED in the message; whether
        # that key is the registered developer key is the next-but-two
        # stage's question (CHECK_DEVELOPER_KEY), per the spec's order.
        pub = str(message.get("developer_key", ""))
        body = f"{message.get('action')}|{message.get('nonce')}"
        return bool(pub) and self.verify(
            body, str(message.get("signature", "")), pub)

    def _verify_nonce(self, message: dict) -> bool:
        nonce = message.get("nonce")
        if not nonce or nonce in self._seen_nonces:
            return False
        self._seen_nonces.add(nonce)
        return True

    def _check_identity_binding(self, message: dict) -> bool:
        if not self.policy.get("identity", {}).get("hardware_binding"):
            return True
        return message.get("device_token") == self.device_token

    def _check_developer_key(self, message: dict) -> bool:
        if not self.policy.get("identity", {}).get("developer_key_required"):
            return True
        return str(message.get("developer_key", "")) == self.trusted_developer_pub

    def _check_airlet_signature(self, message: dict) -> bool:
        if not self.policy.get("identity", {}).get("airlet_signature_required"):
            return True
        bundle_hash = str(message.get("bundle_hash", ""))
        signature = str(message.get("airlet_signature", ""))
        pub = str(message.get("developer_key", ""))
        if not bundle_hash or not signature or not pub:
            return False
        # The Airlet signature is an Ed25519 signature over the bundle's
        # SHA-256 hash (hex string), made by the developer key.
        return self.verify(bundle_hash, signature, pub)

    def check_permission(self, action: str) -> bool:
        """CHECK_PERMISSION — also exposed directly to the VM (CHECK_POLICY)."""
        perm = ACTION_PERMISSIONS.get(action, action)
        return bool(self.policy.get("permissions", {}).get(perm, False))

    def _check_sandbox(self, message: dict) -> bool:
        sandbox = self.policy.get("sandbox", {})
        payload = message.get("payload") or {}
        est_mb = payload.get("est_memory_mb", 1)
        if est_mb > sandbox.get("max_memory_mb", 32):
            return False
        if payload.get("threads") and not sandbox.get("allow_threads", False):
            return False
        if payload.get("subprocess") and not sandbox.get("allow_subprocess", False):
            return False
        return True

    # -- the pipeline ---------------------------------------------------------
    def evaluate(self, message: dict) -> dict:
        """Run the spec's decision pipeline for one AirBridge message.

        Returns {"decision": "ALLOW"|"DENY", "failed_stage": str|None,
                 "stages": [(stage, ok), ...]} and logs the outcome.
        """
        stages = []

        def stage(name, ok):
            stages.append({"stage": name, "ok": bool(ok)})
            return ok

        decision = "ALLOW"
        failed = None
        checks = [
            ("VERIFY_SIGNATURE", lambda: self._verify_signature(message)),
            ("VERIFY_NONCE", lambda: self._verify_nonce(message)),
            ("CHECK_IDENTITY_BINDING", lambda: self._check_identity_binding(message)),
            ("CHECK_DEVELOPER_KEY", lambda: self._check_developer_key(message)),
            ("CHECK_AIRLET_SIGNATURE",
             lambda: self._check_airlet_signature(message)),
            ("CHECK_PERMISSION",
             lambda: self.check_permission(str(message.get("action", "")))),
            ("CHECK_SANDBOX", lambda: self._check_sandbox(message)),
        ]
        for name, fn in checks:
            if not stage(name, fn()):
                decision, failed = "DENY", name
                break
        entry = {
            "action": message.get("action"),
            "decision": decision,
            "failed_stage": failed,
            "stages": stages,
            "final": "POLICY_ALLOW" if decision == "ALLOW" else "POLICY_DENY",
        }
        self.decision_log.append(entry)
        return entry

    # -- Airlet bundle load-time verification --------------------------------
    def verify_bundle(self, bundle_hash: str, signature_hex: str,
                      developer_pub_hex: str) -> dict:
        """Verify a packaged Airlet at load time (Increment 2).

        Checks, in pipeline spirit: the presented developer key is the
        registered one (CHECK_DEVELOPER_KEY), and the bundle signature is a
        valid Ed25519 signature over the bundle hash under that key
        (CHECK_AIRLET_SIGNATURE). The caller recomputes bundle_hash from
        the package bytes, so a tampered package fails here even with the
        original signature. Appends to the decision log and returns the
        verdict entry.
        """
        stages = []

        def stage(name, ok):
            stages.append({"stage": name, "ok": bool(ok)})
            return ok

        decision, failed = "ALLOW", None
        if not stage("CHECK_DEVELOPER_KEY",
                     developer_pub_hex == self.trusted_developer_pub):
            decision, failed = "DENY", "CHECK_DEVELOPER_KEY"
        elif not stage("CHECK_AIRLET_SIGNATURE",
                       self.verify(bundle_hash, signature_hex,
                                   developer_pub_hex)):
            decision, failed = "DENY", "CHECK_AIRLET_SIGNATURE"
        entry = {
            "action": "airlet.load",
            "decision": decision,
            "failed_stage": failed,
            "stages": stages,
            "final": "POLICY_ALLOW" if decision == "ALLOW" else "POLICY_DENY",
            "via": "verify_bundle",
        }
        self.decision_log.append(entry)
        return entry

    # -- VM opcode hooks ------------------------------------------------------
    def vm_check_policy(self, action: str) -> bool:
        """Backs the VM's CHECK_POLICY opcode ([action] -> [allowed])."""
        allowed = self.check_permission(action)
        self.decision_log.append({
            "action": action, "decision": "ALLOW" if allowed else "DENY",
            "failed_stage": None if allowed else "CHECK_PERMISSION",
            "stages": [{"stage": "CHECK_PERMISSION", "ok": allowed}],
            "final": "POLICY_ALLOW" if allowed else "POLICY_DENY",
            "via": "CHECK_POLICY opcode",
        })
        return allowed

    def vm_check_identity(self, token) -> bool:
        """Backs CHECK_IDENTITY (stub: token equals configured device token)."""
        return token == self.device_token

    def vm_check_signature(self, msg) -> bool:
        """Backs CHECK_SIGNATURE: msg is {'body':..,'signature':..[,
        'public_key':..]} — verified with real Ed25519 (Increment 2)."""
        if not isinstance(msg, dict):
            return False
        pub = str(msg.get("public_key") or self.trusted_developer_pub)
        return self.verify(str(msg.get("body", "")),
                            str(msg.get("signature", "")), pub)
