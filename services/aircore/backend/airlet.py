"""
Airlet packaging — Increment 2.

Implements the spec's distributable Airlet package layout:

    MyAirlet/
      AirUI/main.airui          declarative UI (compiles via airui.py)
      AirCore/core.py           the Airlet's core program (PROGRAM_ASM)
      AirBridge/bridge.json     routing table
      AirShield/policy.yaml     policy manifest
      manifest.json             name, version, developer key, bundle
                                hash, and the Ed25519 bundle signature

Bundle hash (canonical form, documented here so verifiers agree):
SHA-256 over, for each bundled file in sorted relative-path order:
    <relative path UTF-8> NUL <file bytes> NUL
manifest.json itself is NOT part of the bundle (it carries the hash and
signature). The bundle signature is an Ed25519 signature over the bundle
hash's hex string, made with the developer key (airshield.verify_bundle
checks it at load time against the recomputed hash — so editing any
bundled file after signing invalidates the package twice over: the hash
no longer matches the manifest, and the signature no longer verifies
against the true hash).

CLI:
    python3 airlet.py keygen                 show the demo developer identity
    python3 airlet.py pack    <pkg_dir>      (re)sign a package in place
    python3 airlet.py verify  <pkg_dir>      load-time verification only
    python3 airlet.py run     <pkg_dir>      verify, then run AirCore/core.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys

from airshield import AirShield, load_policy
from aircore_vm import AirCoreVM, assemble, result_to_dict
from keys import load_or_create_keypair

BUNDLE_FILES = [
    "AirBridge/bridge.json",
    "AirCore/core.py",
    "AirShield/policy.yaml",
    "AirUI/main.airui",
]
MANIFEST = "manifest.json"


def compute_bundle_hash(pkg_dir: str) -> str:
    h = hashlib.sha256()
    for rel in sorted(BUNDLE_FILES):
        with open(os.path.join(pkg_dir, rel), "rb") as fh:
            data = fh.read()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def _airlet_header(pkg_dir: str) -> tuple[str, str]:
    with open(os.path.join(pkg_dir, "AirUI", "main.airui"),
              "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "airlet":
                return parts[1], (parts[2] if len(parts) > 2 else "0")
    return os.path.basename(os.path.abspath(pkg_dir)), "0"


def pack(pkg_dir: str, keypair=None) -> dict:
    """(Re)build manifest.json for a package directory: hash + sign."""
    kp = keypair or load_or_create_keypair()
    name, version = _airlet_header(pkg_dir)
    bundle_hash = compute_bundle_hash(pkg_dir)
    manifest = {
        "name": name,
        "version": version,
        "algorithm": "ed25519",
        "developer_key": kp.public_hex,
        "bundle_hash": bundle_hash,
        "signature": kp.sign(bundle_hash.encode("utf-8")).hex(),
        "files": sorted(BUNDLE_FILES),
    }
    with open(os.path.join(pkg_dir, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return manifest


def read_manifest(pkg_dir: str) -> dict:
    with open(os.path.join(pkg_dir, MANIFEST), "r", encoding="utf-8") as fh:
        return json.load(fh)


def verify_package(pkg_dir: str, shield: AirShield) -> dict:
    """Load-time verification through AirShield. Returns a verdict dict:
    {"decision", "failed_stage", "detail", "manifest"}."""
    manifest = read_manifest(pkg_dir)
    actual_hash = compute_bundle_hash(pkg_dir)
    if actual_hash != manifest.get("bundle_hash"):
        verdict = {
            "decision": "DENY",
            "failed_stage": "CHECK_AIRLET_SIGNATURE",
            "detail": "bundle hash mismatch — package contents changed "
                      "after signing (tampered or corrupted)",
            "manifest": manifest,
        }
        shield.decision_log.append({
            "action": "airlet.load", "decision": "DENY",
            "failed_stage": "CHECK_AIRLET_SIGNATURE",
            "stages": [{"stage": "CHECK_AIRLET_SIGNATURE", "ok": False}],
            "final": "POLICY_DENY", "via": "verify_package (hash mismatch)",
        })
        return verdict
    entry = shield.verify_bundle(actual_hash,
                                 str(manifest.get("signature", "")),
                                 str(manifest.get("developer_key", "")))
    return {
        "decision": entry["decision"],
        "failed_stage": entry["failed_stage"],
        "detail": "bundle signature verified (Ed25519 over bundle hash)"
                  if entry["decision"] == "ALLOW"
                  else f"signature verification failed at {entry['failed_stage']}",
        "manifest": manifest,
    }


def extract_program_asm(pkg_dir: str) -> str:
    """Pull PROGRAM_ASM out of AirCore/core.py WITHOUT executing it:
    parse the file with ast and literal-eval the assignment."""
    path = os.path.join(pkg_dir, "AirCore", "core.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PROGRAM_ASM"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"{path}: no PROGRAM_ASM assignment found")


def load_and_run(pkg_dir: str, shield: AirShield | None = None) -> dict:
    """Verify a package through AirShield, then run its core program on
    the VM (wired to the shield's hooks). Denied packages never execute."""
    if shield is None:
        policy_path = os.path.join(pkg_dir, "AirShield", "policy.yaml")
        shield = AirShield(load_policy(policy_path))
    verdict = verify_package(pkg_dir, shield)
    if verdict["decision"] != "ALLOW":
        return {"verified": False, "verdict": verdict}
    program = assemble(extract_program_asm(pkg_dir))
    vm = AirCoreVM(policy_check=shield.vm_check_policy,
                   identity_check=shield.vm_check_identity,
                   signature_check=shield.vm_check_signature)
    result = vm.run(program)
    return {"verified": True, "verdict": verdict,
            "result": result_to_dict(result, program)}


def _cli(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "keygen":
        kp = load_or_create_keypair()
        print(f"demo developer public key: {kp.public_hex}")
        print("(seed stays in backend/keys/developer.seed — demo only)")
        return 0
    pkg_dir = argv[2]
    if cmd == "pack":
        manifest = pack(pkg_dir)
        print(f"packed {manifest['name']} v{manifest['version']}")
        print(f"  bundle_hash: {manifest['bundle_hash']}")
        print(f"  signature:   {manifest['signature'][:32]}…")
        return 0
    if cmd == "verify":
        shield = AirShield(load_policy(
            os.path.join(pkg_dir, "AirShield", "policy.yaml")))
        verdict = verify_package(pkg_dir, shield)
        print(json.dumps({k: verdict[k] for k in
                          ("decision", "failed_stage", "detail")}, indent=2))
        return 0 if verdict["decision"] == "ALLOW" else 1
    if cmd == "run":
        out = load_and_run(pkg_dir)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["verified"] else 1
    print(f"unknown command {cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
