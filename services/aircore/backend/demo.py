#!/usr/bin/env python3
"""
End-to-end CLI demo: AirUI-equivalent client -> AirBridge -> AirShield -> VM.

Run:  python3 demo.py
Shows the demo Airlet executing on the AirCore VM, the full step trace, the
AirShield decision log, and a deliberately denied "network" action.
"""

import json

from airbridge import build_bridge


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, indent=2, default=str))


def main():
    bridge = build_bridge()

    # 1. Execute the demo Airlet through the bus (route: execute -> core.execute)
    envelope = bridge.send(bridge.make_message("execute"))
    result = envelope["result"]
    show("AirBridge envelope (execute)", {
        "routed_to": envelope.get("routed_to"),
        "policy_decision": envelope["policy"]["decision"],
    })
    show("VM return value", result["return_value"])
    print("\n=== Execution trace (step: pc opcode -> stack | locals) ===")
    for s in result["trace"]:
        print(f"  {s['step']:>2}. pc={s['pc']:<2} {s['opcode']:<14} "
              f"stack={s['stack']}  locals={s['locals']}")
    print(f"final stack: {result['final_stack']}   halted: {result['halted']}")

    # 2. The spec's diagnostics route
    diag = bridge.send(bridge.make_message("diagnostics"))
    show("Route 'diagnostics' -> core.diagnostics", diag["result"])

    # 3. Zero-trust in action: 'network' is routed nowhere and policy denies it
    denied = bridge.send(bridge.make_message("network"))
    show("Action 'network' (must be denied)",
         {"decision": denied["policy"]["decision"],
          "failed_stage": denied["policy"]["failed_stage"],
          "error": denied.get("error")})

    # 4. AirShield's full decision log for the session
    show("AirShield decision log", bridge.shield.decision_log)

    # Assertions so this demo doubles as a smoke test.
    rv = result["return_value"]
    assert rv["y"] == 7, rv
    assert rv["diag_allowed"] is True, rv
    assert rv["net_allowed"] is False, rv
    assert rv["diag"] == {"cpu": "2%", "memory": "14MB", "sandbox": "clean"}, rv
    assert denied["policy"]["decision"] == "DENY"
    print("\nALL CHECKS PASSED ✔  (y=7, diag allowed, network denied, "
          "diagnostics payload matches spec)")


if __name__ == "__main__":
    main()
