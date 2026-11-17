# BlackBerry LAN

The Air platform core from `BlackBerry_LAN.pdf` (Taylor's design document:
AirCore stack VM, AirBridge message bus, AirShield zero-trust IPC, AirUI,
Airlets — plus a BlackBerry 10 / QNX + Python handset blueprint), built as a
fleet service at Taylor's direction ("Install", 2026-11-17).

What runs here (Python stdlib only, port 8771, base path `/blackberry`):

- **AirCore VM** — executes the document's opcode map (PUSH/POP/DUP/SWAP,
  ADD/SUB/MUL/DIV, LOAD/STORE, JMP/JZ, CALL/RET, HALT), step-limited to
  10,000, no network opcodes. `POST /blackberry/aircore/run`.
- **AirBridge** — local message bus honoring the document's route table
  `{"diagnostics": "core.diagnostics"}` plus an `echo` channel.
  `POST /blackberry/bridge/send`, `GET /blackberry/bridge/messages`.
- **AirShield** — the document's policy `{"aircore_exec": true,
  "network": false}`, enforced: unknown opcodes and unrouted channels are
  rejected. `GET /blackberry/shield`.
- **Diagnostics** — `GET /blackberry/diagnostics` in the document's shape
  (cpu / memory / sandbox), reporting the QNX design-target sandbox path
  alongside the live container path.

Not claimed: the document's handset layers (Cascades/QML UI, C++ bridge,
embedded Python in a signed .bar, debug tokens) target a BlackBerry 10
device and are design material, not server-runnable code.
