"""
AirBridge — secure message bus (in-process STUB of the spec's bus).

Per the spec (BlackBerry LAN.docx, "AIRBRIDGE — MESSAGE BUS SPEC"):
  * AirBridge is the ONLY communication channel between UI and logic.
  * Messages are signed JSON: {"action", "payload", "nonce", "signature"}.
  * Routing is policy-checked: every message passes AirShield before it is
    routed, using the routing table in bridge.json.
  * Results return to the UI as structured output (AirBridge.state).

Increment 2 (2026-11-09): message and bundle signing is real Ed25519 via
AirShield (see airshield.py / ed25519.py). The bus remains single-process
and local — no network transport; the HTTP layer in server.py is a demo
harness, not the bus itself — but it now has the spec's async surface:
`send_async()` (asyncio) plus reactive AirBridge.state: subscribers
registered with subscribe() are notified whenever state changes after a
routed message, which is what AirUI bindings (Text(AirBridge.state.output))
consume.
"""

from __future__ import annotations

import asyncio
import json
import os

from aircore_vm import AirCoreVM, assemble, result_to_dict
from airshield import AirShield, DEMO_DEVICE_TOKEN

HERE = os.path.dirname(os.path.abspath(__file__))


class AirBridge:
    def __init__(self, shield: AirShield, routes: dict, demo_source: str):
        self.shield = shield
        self.routes = routes
        self.demo_source = demo_source
        self.state = {"output": None, "result": None}   # AirBridge.state.*
        self.message_log = []
        self._subscribers = []   # reactive AirBridge.state bindings
        self.handlers = {
            "core.execute": self._core_execute,
            "core.status": self._core_status,
            "core.diagnostics": self._core_diagnostics,
        }

    # -- message construction -------------------------------------------------
    def bundle_hash(self) -> str:
        """SHA-256 of the Airlet bundle this bridge serves (the demo
        program source stands in for the bundle in this in-process bus;
        packaged Airlets hash their whole package — see airlet.py)."""
        return self.shield.hash_bundle(self.demo_source.encode("utf-8"))

    def make_message(self, action: str, payload: dict | None = None) -> dict:
        """Build a spec-shaped AirBridge message, signed with real
        Ed25519 (Increment 2): the message signature covers
        "action|nonce"; the Airlet signature covers the bundle hash;
        developer_key is the signer's Ed25519 public key (hex)."""
        nonce = self.shield.new_nonce()
        bundle_hash = self.bundle_hash()
        return {
            "action": action,
            "payload": payload or {},
            "nonce": nonce,
            "signature": self.shield.sign(f"{action}|{nonce}"),
            "developer_key": self.shield.keypair.public_hex,
            "bundle_hash": bundle_hash,
            "airlet_signature": self.shield.sign(bundle_hash),
            # Stub identity material (no hardware root of trust):
            "device_token": DEMO_DEVICE_TOKEN,
        }

    # -- the bus ---------------------------------------------------------------
    def send(self, message: dict) -> dict:
        """Validate with AirShield, route, execute, return structured output."""
        verdict = self.shield.evaluate(message)
        envelope = {"request": {k: message.get(k) for k in
                                ("action", "nonce")},
                    "policy": verdict}
        if verdict["decision"] == "DENY":
            envelope["error"] = (
                f"POLICY_DENY at stage {verdict['failed_stage']}")
            self.message_log.append(envelope)
            return envelope

        target = self.routes.get(str(message.get("action")))
        if target is None or target not in self.handlers:
            envelope["error"] = f"No route for action '{message.get('action')}'"
            self.message_log.append(envelope)
            return envelope

        envelope["routed_to"] = target
        result = self.handlers[target](message.get("payload") or {})
        envelope["result"] = result
        self.state["result"] = result
        self.state["output"] = result
        self.message_log.append(envelope)
        self._notify_subscribers()
        return envelope

    # -- reactive state + async surface (Increment 2) -------------------------
    def subscribe(self, callback) -> None:
        """Register a reactive binding: callback(state_copy) fires after
        every routed message updates AirBridge.state. This is the binding
        an AirUI `Text(AirBridge.state.output)` compiles against."""
        self._subscribers.append(callback)

    def _notify_subscribers(self) -> None:
        snapshot = dict(self.state)
        for cb in list(self._subscribers):
            cb(snapshot)

    async def send_async(self, message: dict) -> dict:
        """Async AirBridge send (spec's event-loop surface).

        The policy pipeline and VM are synchronous CPU work, so the send
        runs via asyncio.to_thread; callers can pipeline many messages
        with asyncio.gather and each still passes AirShield in order of
        completion. Note: AirShield's nonce registry is a plain set —
        adequate for this local demo, not a concurrent production store.
        """
        return await asyncio.to_thread(self.send, message)

    # -- core handlers -----------------------------------------------------------
    def _new_vm(self) -> AirCoreVM:
        return AirCoreVM(
            policy_check=self.shield.vm_check_policy,
            identity_check=self.shield.vm_check_identity,
            signature_check=self.shield.vm_check_signature,
        )

    def _core_execute(self, payload: dict) -> dict:
        source = payload.get("source") or self.demo_source
        program = assemble(source)
        result = self._new_vm().run(program)
        return result_to_dict(result, program)

    def _core_status(self, payload: dict) -> dict:
        return {
            "vm": "AirCore VM (stack-based)",
            "bridge": "AirBridge (policy-checked routing)",
            "shield": "AirShield (stub policy engine)",
            "routes": self.routes,
            "messages_processed": len(self.message_log),
        }

    def _core_diagnostics(self, payload: dict) -> dict:
        # Runs the spec's own example bytecode path:
        #   LOAD_NAME diagnostics / CALL_FUNCTION 0 / RETURN_VALUE
        program = assemble("LOAD_NAME diagnostics\nCALL_FUNCTION 0\nRETURN_VALUE\n")
        result = self._new_vm().run(program)
        return result.return_value


def build_bridge() -> AirBridge:
    """Wire up AirShield + AirBridge from the files shipped in this folder."""
    from airshield import load_policy
    policy = load_policy(os.path.join(HERE, "policy.yaml"))
    with open(os.path.join(HERE, "bridge.json"), "r", encoding="utf-8") as fh:
        routes = json.load(fh)["routes"]
    with open(os.path.join(HERE, "demo.airasm"), "r", encoding="utf-8") as fh:
        demo_source = fh.read()
    return AirBridge(AirShield(policy), routes, demo_source)
