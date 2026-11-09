# AirCore — BlackBerry LAN “AirCore” Platform (Increments 1–2)

A runnable first increment of the BlackBerry Air platform from the omnibus
spec in Google Drive (`BlackBerry LAN.docx`): a stack-based **AirCore VM**
executing the spec's opcode map, an **AirBridge** message-bus stub with
policy-checked routing, an **AirShield** zero-trust policy-engine stub, and
an **AirUI** web shell that runs programs on the VM and shows its state.

Everything runs locally with the Python 3 standard library only.
No deploys, no accounts, no network services beyond a localhost demo server.

## Layout

```
aircore/
├── backend/
│   ├── aircore_vm.py   # the VM + assembler (spec opcode map)
│   ├── ed25519.py      # Ed25519 signatures, pure Python (RFC 8032) [Inc 2]
│   ├── keys.py         # demo developer identity keystore [Inc 2]
│   ├── keys/           # developer.seed / developer.pub (demo identity) [Inc 2]
│   ├── airshield.py    # policy engine (spec decision pipeline, Ed25519)
│   ├── airbridge.py    # message bus (bridge.json routing, async + reactive state)
│   ├── airui.py        # .airui parser/compiler -> AirCore bytecode [Inc 2]
│   ├── airlet.py       # Airlet pack / verify / run tooling [Inc 2]
│   ├── bridge.json     # routing table, from the spec
│   ├── policy.yaml     # AirShield manifest, the spec's expanded model
│   ├── demo.airasm     # demo Airlet program (spec's own examples)
│   ├── demo.py         # CLI end-to-end demo / smoke test
│   ├── test_increment2.py  # Increment 2 verification suite (22 checks)
│   └── server.py       # localhost HTTP API + static host for the AirUI
├── airlets/
│   └── MyAirlet/       # packaged, signed demo Airlet (spec layout) [Inc 2]
│       ├── AirUI/main.airui · AirCore/core.py
│       ├── AirBridge/bridge.json · AirShield/policy.yaml · manifest.json
├── frontend/
│   └── index.html      # AirUI shell (single file, no build step)
└── demo/
    └── index.html      # self-contained static demo (VM re-implemented in JS)
```

## Run it

CLI (fastest check — runs the demo Airlet through AirBridge → AirShield →
VM, prints the full execution trace and the policy decision log, and asserts
the expected results):

```bash
cd backend
python3 demo.py
```

Expected tail: `ALL CHECKS PASSED ✔ (y=7, diag allowed, network denied, ...)`

Web UI (AirUI shell):

```bash
cd backend
python3 server.py          # http://127.0.0.1:8765
```

Open the URL, edit the assembly if you like, and use the buttons:
**Run via AirBridge (execute)** runs the program on the VM and shows the
return value, final stack, locals, step-by-step trace, and AirShield's
decision log. **diagnostics** / **status** exercise the other routes.
**Try action: network** demonstrates a POLICY_DENY (network is `false` in
`policy.yaml`).

## The demo program

`demo.airasm` is assembled from the spec's own examples: the trace example
(`x = 5; if x > 3: y = x + 2`) and the `diagnostics()` call
(`LOAD_NAME diagnostics / CALL_FUNCTION 0 / RETURN_VALUE`), extended with
`CHECK_POLICY` so AirShield decides `diagnostics` (allow) and `network`
(deny) live inside the VM. It returns
`{"y": 7, "diag_allowed": true, "net_allowed": false, "diag": {...}}`.

## Opcode map

Canonical IDs 0x01–0x0A are the spec's numbered table verbatim
(LOAD_CONST … POP_TOP). The spec's expanded map (COMPARE_LT/GT, BINARY_*,
BUILD_LIST, LIST_APPEND, MAP_SET, CHECK_POLICY / CHECK_IDENTITY /
CHECK_SIGNATURE, NOP, HALT) and the trace example's LOAD_FAST / STORE_FAST
are implemented with implementation-assigned IDs 0x0B–0x18 / 0x00 / 0xFF —
see the header of `aircore_vm.py`. The spec corpus also contains a
conflicting fragment (0x09 = STORE_FAST); this build follows the spec's
clean summary table.

## Increment 2 (2026-11-09) — what changed

- **Real Ed25519 everywhere.** `ed25519.py` implements RFC 8032 in pure
  Python (no crypto library exists in this environment; verified against
  the RFC 8032 §7.1 test vector). A demo developer identity is generated
  locally on first use (`keys.py`, persisted in `backend/keys/`).
  AirShield now verifies message signatures, the developer key, and the
  Airlet bundle signature cryptographically; the VM's CHECK_SIGNATURE
  hook verifies too. The HMAC stand-in is gone.
- **Airlet packaging.** `airlets/MyAirlet/` is a signed package in the
  spec layout with `manifest.json` (bundle hash + Ed25519 signature over
  the hash). `airlet.py` packs, verifies (through AirShield, at load
  time), and runs packages; a tampered package is rejected before it can
  execute — see the test suite for the three rejection modes.
- **`.airui` compiler.** `airui.py` compiles declarative AirUI
  (view tree + handler statements/expressions) to AirCore bytecode.
  `text bind state.output` compiles to a `LOAD_NAME state.output`, so
  renders resolve live AirBridge.state bindings. DemoAirlet's
  `main.airui` compiles to a `run_demo` handler that reproduces the spec
  demo result exactly.
- **Async AirBridge + reactive state.** `send_async()` (asyncio) and
  `subscribe()` notifications on AirBridge.state changes — the surface
  AirUI bindings consume.

Verify everything: `cd backend && python3 test_increment2.py`
(22 checks: RFC vector, package verify + 3 tamper rejections, demo.py
regression, compiler output execution, async bridge, bad-signature
denial). Pack/verify/run a package: `python3 airlet.py pack|verify|run
../airlets/MyAirlet`. Compile an `.airui`: `python3 airui.py
../airlets/MyAirlet/AirUI/main.airui`.

**Static demo:** `demo/index.html` is fully self-contained (the VM,
assembler, and policy pipeline re-implemented in JavaScript) for
embedding in a static website. Its JS core was verified headlessly under
Node: same 29-step trace and results as the Python build. Signature
stages are labelled *simulated* there — a web page holds no private key;
cryptographic verification lives in the Python build.

## What is stubbed (honest list, updated for Increment 2)

- **Ed25519 implementation** — genuine RFC 8032 and test-vector-verified,
  but pure Python: slow and **not constant-time**. Demo-grade; swap in a
  vetted library (PyNaCl / cryptography) behind the same API for
  production.
- **Demo key custody** — the developer seed is a plaintext local file
  (`backend/keys/developer.seed`), generated for this demo. AirShield
  trusts that same identity, collapsing the developer/platform roles a
  production build would separate. No hardware root of trust; identity
  binding still compares a demo device token.
- **AirBridge transport** — still in-process and local; `send_async`
  offloads to a thread (asyncio.to_thread) rather than being a native
  async event loop, there is no encrypted transport, and the nonce
  registry is a plain in-memory set. `server.py` remains a localhost
  demo harness.
- **AirUI shell (frontend/)** — unchanged hand-written console page; the
  `.airui` compiler exists, but the shell does not yet render compiled
  `.airui` views (the static demo shows the compiled model in-browser).
- **AirBase microkernel, sandbox resource enforcement** (memory/CPU caps
  are policy values and a VM step limit, not OS-level enforcement),
  encrypted containers, and encrypted bytecode are **not implemented**.

## Next increments

1. Sandbox enforcement: memory/CPU accounting inside the VM, encrypted
   bytecode at rest.
2. AirUI shell rendering compiled `.airui` view trees with live
   AirBridge.state bindings.
3. Native async AirBridge event loop + encrypted transport.
4. Vetted Ed25519 library swap and separated developer/platform keys
   with hardware-backed storage.
