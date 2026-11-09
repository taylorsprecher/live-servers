#!/usr/bin/env python3
"""
Increment 2 verification suite.

Run:  python3 test_increment2.py

Covers, per the Increment 2 task definition:
  1. Ed25519 correctness against the RFC 8032 section 7.1 test vector.
  2. A packaged Airlet (airlets/MyAirlet) loads, verifies via AirShield,
     and runs — spec demo result.
  3. Tampered Airlets are rejected: (a) modified bundle file -> hash
     mismatch DENY; (b) forged manifest signature -> CHECK_AIRLET_SIGNATURE
     DENY; (c) unregistered developer key -> CHECK_DEVELOPER_KEY DENY.
  4. The Increment 1 demo (demo.py) still passes.
  5. The .airui compiler: DemoAirlet's main.airui compiles; its run_demo
     handler executes on the VM with the spec result; the __view__
     program builds the view tree and resolves the state.output binding.
  6. Async AirBridge: send_async + reactive-state subscribers.
  7. A message with a corrupted Ed25519 signature is denied at
     VERIFY_SIGNATURE.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ed25519 as ed                      # noqa: E402
import airlet                              # noqa: E402
from airbridge import build_bridge         # noqa: E402
from aircore_vm import AirCoreVM           # noqa: E402
from airshield import AirShield, load_policy  # noqa: E402
from airui import compile_airui            # noqa: E402

PKG = os.path.join(HERE, "..", "airlets", "MyAirlet")
EXPECTED = {"y": 7, "diag_allowed": True, "net_allowed": False,
            "diag": {"cpu": "2%", "memory": "14MB", "sandbox": "clean"}}

passed = []


def check(name, cond, detail=""):
    assert cond, f"FAILED: {name} {detail}"
    passed.append(name)
    print(f"  ok  {name}")


def fresh_shield():
    return AirShield(load_policy(os.path.join(PKG, "AirShield", "policy.yaml")))


print("1. Ed25519 — RFC 8032 test vector")
seed = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
kp = ed.KeyPair(seed)
check("public key matches RFC vector",
      kp.public_hex == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
sig = kp.sign(b"")
check("signature matches RFC vector",
      sig.hex() == "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                   "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
check("verify accepts / rejects tampered message",
      ed.verify(kp.public_key, b"", sig) and not ed.verify(kp.public_key, b"x", sig))

print("2. Packaged Airlet loads, verifies, runs")
out = airlet.load_and_run(PKG)
check("package verified", out["verified"] is True, str(out.get("verdict")))
check("core program result matches spec demo",
      out["result"]["return_value"] == EXPECTED,
      str(out["result"]["return_value"]))

print("3. Tampered Airlets are rejected")
tmp = tempfile.mkdtemp(prefix="airlet-tamper-")
try:
    # (a) modify a bundled file after signing
    pkg_a = os.path.join(tmp, "a")
    shutil.copytree(PKG, pkg_a)
    core_path = os.path.join(pkg_a, "AirCore", "core.py")
    with open(core_path, "a", encoding="utf-8") as fh:
        fh.write("\n# tampered\n")
    v = airlet.verify_package(pkg_a, fresh_shield())
    check("modified bundle -> DENY (hash mismatch)",
          v["decision"] == "DENY" and "hash mismatch" in v["detail"], str(v))
    out_a = airlet.load_and_run(pkg_a, fresh_shield())
    check("tampered package never executes", out_a["verified"] is False)

    # (b) forge the manifest signature (hash intact)
    pkg_b = os.path.join(tmp, "b")
    shutil.copytree(PKG, pkg_b)
    manifest = airlet.read_manifest(pkg_b)
    manifest["signature"] = "00" * 64
    with open(os.path.join(pkg_b, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    v = airlet.verify_package(pkg_b, fresh_shield())
    check("forged signature -> DENY at CHECK_AIRLET_SIGNATURE",
          v["decision"] == "DENY"
          and v["failed_stage"] == "CHECK_AIRLET_SIGNATURE", str(v))

    # (c) sign with an unregistered developer key
    pkg_c = os.path.join(tmp, "c")
    shutil.copytree(PKG, pkg_c)
    rogue = ed.KeyPair.generate()
    manifest = airlet.pack(pkg_c, keypair=rogue)
    v = airlet.verify_package(pkg_c, fresh_shield())
    check("unregistered developer key -> DENY at CHECK_DEVELOPER_KEY",
          v["decision"] == "DENY"
          and v["failed_stage"] == "CHECK_DEVELOPER_KEY", str(v))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("4. Increment 1 demo still passes")
proc = subprocess.run([sys.executable, os.path.join(HERE, "demo.py")],
                      capture_output=True, text=True, cwd=HERE)
check("demo.py exit 0 with ALL CHECKS PASSED",
      proc.returncode == 0 and "ALL CHECKS PASSED" in proc.stdout,
      proc.stdout[-300:] + proc.stderr[-300:])

print("5. .airui compiler — output executes")
with open(os.path.join(PKG, "AirUI", "main.airui"), encoding="utf-8") as fh:
    compiled = compile_airui(fh.read())
check("compiled header", compiled.name == "DemoAirlet" and compiled.version == "1.0")
check("handlers compiled", set(compiled.handlers) ==
      {"run_demo", "diagnostics", "network"})
shield = fresh_shield()
vm = AirCoreVM(policy_check=shield.vm_check_policy,
               identity_check=shield.vm_check_identity,
               signature_check=shield.vm_check_signature)
rv = vm.run(compiled.handlers["run_demo"]).return_value
check("run_demo handler result matches spec demo", rv == EXPECTED, str(rv))
rv2 = vm.run(compiled.handlers["diagnostics"]).return_value
check("diagnostics handler returns spec payload",
      rv2 == {"cpu": "2%", "memory": "14MB", "sandbox": "clean"}, str(rv2))
vm_state = AirCoreVM(builtins={"state.output": {"y": 7}})
tree = vm_state.run(compiled.view_program).return_value
check("view program builds a column tree",
      tree["type"] == "column" and len(tree["children"]) == 5, str(tree)[:200])
bound = tree["children"][1]
check("state.output binding resolved in view tree",
      bound.get("bind") == "state.output" and bound.get("value") == {"y": 7},
      str(bound))
actions = [c.get("action") for c in tree["children"] if c["type"] == "button"]
check("buttons carry handler actions",
      actions == ["run_demo", "diagnostics", "network"], str(actions))

print("6. Async AirBridge + reactive state")
bridge = build_bridge()
seen = []
bridge.subscribe(lambda state: seen.append(state["output"]))


async def run_async():
    env1, env2, env3 = await asyncio.gather(
        bridge.send_async(bridge.make_message("execute")),
        bridge.send_async(bridge.make_message("diagnostics")),
        bridge.send_async(bridge.make_message("network")),
    )
    return env1, env2, env3


env1, env2, env3 = asyncio.run(run_async())
check("async execute allowed + routed",
      env1["policy"]["decision"] == "ALLOW"
      and env1["result"]["return_value"] == EXPECTED)
check("async diagnostics allowed", env2["policy"]["decision"] == "ALLOW")
check("async network denied at CHECK_PERMISSION",
      env3["policy"]["decision"] == "DENY"
      and env3["policy"]["failed_stage"] == "CHECK_PERMISSION")
check("subscribers notified of state changes", len(seen) >= 2, str(len(seen)))

print("7. Corrupted message signature denied at VERIFY_SIGNATURE")
bridge2 = build_bridge()
msg = bridge2.make_message("execute")
msg["signature"] = "ff" * 64
env = bridge2.send(msg)
check("bad Ed25519 signature -> DENY at VERIFY_SIGNATURE",
      env["policy"]["decision"] == "DENY"
      and env["policy"]["failed_stage"] == "VERIFY_SIGNATURE", str(env["policy"]))

print(f"\nALL {len(passed)} INCREMENT-2 CHECKS PASSED ✔")
