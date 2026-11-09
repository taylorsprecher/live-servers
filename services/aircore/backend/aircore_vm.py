"""
AirCore VM — stack-based virtual machine for the BlackBerry Air platform.

Implements the AirCore VM opcode map from the "BlackBerry LAN" omnibus spec
(Drive: BlackBerry LAN.docx).

Canonical opcodes 0x01-0x0A use the spec's own numbered table verbatim:

    0x01 LOAD_CONST     [] -> [const]          push constant from const table
    0x02 LOAD_NAME      [] -> [name]           push name (function/variable)
    0x03 CALL_FUNCTION  [fn, args...] -> [result]
    0x04 RETURN_VALUE   [value] -> []          return top of stack to AirBridge
    0x05 LOAD_ATTR      [obj] -> [attr]        safe, read-only attribute load
    0x06 BUILD_MAP      [k1,v1,...] -> [{...}] build dict from key/value pairs
    0x07 COMPARE_EQ     [a,b] -> [bool]
    0x08 JUMP_IF_FALSE  [cond] -> []           conditional jump
    0x09 JUMP           [] -> []               unconditional jump
    0x0A POP_TOP        [x] -> []              pop and discard

The spec's expanded map names further opcodes without numbering them, and the
spec's execution-trace example uses LOAD_FAST / STORE_FAST locals. Those are
implemented here with implementation-assigned IDs (0x0B+), documented in
Build_Brief.md:

    0x0B COMPARE_LT     0x0C COMPARE_GT
    0x0D BINARY_ADD     0x0E BINARY_SUB     0x0F BINARY_MUL     0x10 BINARY_DIV
    0x11 BUILD_LIST     0x12 LIST_APPEND    0x13 MAP_SET
    0x14 CHECK_POLICY   [action] -> [allowed]   (AirShield hook)
    0x15 CHECK_IDENTITY [token] -> [valid]      (AirShield hook, stub)
    0x16 CHECK_SIGNATURE [msg] -> [verified]    (AirShield hook, stub)
    0x17 LOAD_FAST      push local variable
    0x18 STORE_FAST     pop into local variable
    0x00 NOP            0xFF HALT

Note: the spec corpus contains a second, conflicting fragment that numbers
0x09 as STORE_FAST and 0x0A as BINARY_ADD. This implementation follows the
spec's clean summary table (0x09 JUMP / 0x0A POP_TOP) as canonical.

Sandbox notes (per spec): arithmetic is numeric-only, attribute access is
read-only, there is no filesystem / network / subprocess access from bytecode,
and a step limit caps runaway programs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Opcode table: mnemonic -> (id, description)
# --------------------------------------------------------------------------

OPCODES = {
    "NOP":            (0x00, "No operation"),
    "LOAD_CONST":     (0x01, "Push constant from const table"),
    "LOAD_NAME":      (0x02, "Push name (function/variable)"),
    "CALL_FUNCTION":  (0x03, "Call function with N args"),
    "RETURN_VALUE":   (0x04, "Return top of stack"),
    "LOAD_ATTR":      (0x05, "Load attribute from object"),
    "BUILD_MAP":      (0x06, "Build dict from key/value pairs"),
    "COMPARE_EQ":     (0x07, "Compare equality"),
    "JUMP_IF_FALSE":  (0x08, "Conditional jump on false"),
    "JUMP":           (0x09, "Unconditional jump"),
    "POP_TOP":        (0x0A, "Pop and discard top of stack"),
    # --- expanded map (IDs assigned by this implementation) ---
    "COMPARE_LT":     (0x0B, "Less-than comparison (numeric)"),
    "COMPARE_GT":     (0x0C, "Greater-than comparison (numeric)"),
    "BINARY_ADD":     (0x0D, "Add two values"),
    "BINARY_SUB":     (0x0E, "Subtract"),
    "BINARY_MUL":     (0x0F, "Multiply"),
    "BINARY_DIV":     (0x10, "Divide"),
    "BUILD_LIST":     (0x11, "Build list from N items"),
    "LIST_APPEND":    (0x12, "Append item to list"),
    "MAP_SET":        (0x13, "Set key/value in map"),
    "CHECK_POLICY":   (0x14, "AirShield policy check: [action] -> [allowed]"),
    "CHECK_IDENTITY": (0x15, "AirShield identity check (stub)"),
    "CHECK_SIGNATURE": (0x16, "AirShield signature check (stub)"),
    "LOAD_FAST":      (0x17, "Push local variable"),
    "STORE_FAST":     (0x18, "Pop into local variable"),
    "HALT":           (0xFF, "Stop VM execution"),
}

ID_TO_NAME = {v[0]: k for k, v in OPCODES.items()}

# Instructions whose operand is a jump target label in assembly source.
JUMP_OPS = {"JUMP", "JUMP_IF_FALSE"}
# Instructions whose operand is a literal constant (JSON-parsed).
CONST_OPS = {"LOAD_CONST"}


class VMError(Exception):
    """Raised for any bytecode-level fault (underflow, bad name, type error)."""


# --------------------------------------------------------------------------
# Assembler: text assembly -> list of (opcode_id, operand)
# --------------------------------------------------------------------------

@dataclass
class Program:
    instructions: list          # list of (opcode_id:int, operand)
    source_lines: list = field(default_factory=list)  # per-instruction source text


def assemble(source: str) -> Program:
    """Assemble AirCore assembly text into a Program.

    Syntax, one instruction per line:
        MNEMONIC [operand]
        label:                (defines a jump target)
        ; comment             (also # comments)
    LOAD_CONST takes a JSON literal:  LOAD_CONST 5   LOAD_CONST "text"
    Jump instructions take a label:   JUMP_IF_FALSE skip
    """
    labels = {}
    parsed = []  # (mnemonic, raw_operand, lineno, text)

    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.endswith(":") and " " not in line[:-1]:
            name = line[:-1]
            if name in labels:
                raise VMError(f"line {lineno}: duplicate label '{name}'")
            labels[name] = len(parsed)
            continue
        parts = line.split(None, 1)
        mnemonic = parts[0].upper()
        operand = parts[1].strip() if len(parts) > 1 else None
        if mnemonic not in OPCODES:
            raise VMError(f"line {lineno}: unknown mnemonic '{mnemonic}'")
        parsed.append((mnemonic, operand, lineno, line))

    instructions = []
    source_lines = []
    for mnemonic, operand, lineno, text in parsed:
        op_id = OPCODES[mnemonic][0]
        if mnemonic in JUMP_OPS:
            if operand not in labels:
                raise VMError(f"line {lineno}: unknown label '{operand}'")
            value = labels[operand]
        elif mnemonic in CONST_OPS:
            try:
                value = json.loads(operand)
            except (TypeError, json.JSONDecodeError):
                value = operand  # bare word treated as a string constant
        elif mnemonic in ("CALL_FUNCTION", "BUILD_MAP", "BUILD_LIST"):
            value = int(operand)
        elif mnemonic in ("LOAD_NAME", "LOAD_FAST", "STORE_FAST", "LOAD_ATTR"):
            value = operand.strip("\"'")
        else:
            value = operand
        instructions.append((op_id, value))
        source_lines.append(text)
    return Program(instructions=instructions, source_lines=source_lines)


def disassemble(program: Program) -> list:
    """Human-readable listing of a Program (used in traces and the UI)."""
    out = []
    for pc, (op_id, operand) in enumerate(program.instructions):
        name = ID_TO_NAME.get(op_id, f"0x{op_id:02X}")
        text = program.source_lines[pc] if pc < len(program.source_lines) else name
        out.append({"pc": pc, "opcode": name, "opcode_id": f"0x{op_id:02X}",
                    "operand": operand, "text": text})
    return out


# --------------------------------------------------------------------------
# The VM
# --------------------------------------------------------------------------

def _spec_diagnostics():
    """The spec's own example AirCore function (BlackBerry LAN.docx)."""
    return {"cpu": "2%", "memory": "14MB", "sandbox": "clean"}


def _spec_execute():
    """The spec's AirCore SDK example module function."""
    return {"status": "ok", "message": "AirCore executed."}


DEFAULT_BUILTINS = {
    "diagnostics": _spec_diagnostics,
    "execute": _spec_execute,
}

MAX_STEPS = 10_000  # sandbox cap (spec sandbox: bounded execution)


@dataclass
class TraceStep:
    pc: int
    opcode: str
    stack: list        # stack snapshot AFTER executing (repr strings)
    locals: dict       # locals snapshot AFTER executing


@dataclass
class RunResult:
    return_value: object
    stack: list
    locals: dict
    trace: list        # list[TraceStep]
    halted: bool


class AirCoreVM:
    """Stack-based VM. `policy_check` is an AirShield hook:
    callable(action:str) -> bool. If None, CHECK_POLICY denies by default
    (zero-trust: no policy engine attached means nothing is allowed).
    """

    def __init__(self, builtins=None, policy_check=None, identity_check=None,
                 signature_check=None):
        self.builtins = dict(DEFAULT_BUILTINS)
        if builtins:
            self.builtins.update(builtins)
        self.policy_check = policy_check
        self.identity_check = identity_check
        self.signature_check = signature_check

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _pop(stack):
        if not stack:
            raise VMError("stack underflow")
        return stack.pop()

    @staticmethod
    def _numeric(op, a, b):
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
            raise VMError(f"{op}: arithmetic/comparison is numeric-only "
                          f"(got {type(a).__name__}, {type(b).__name__})")
        return a, b

    # -- execution ---------------------------------------------------------
    def run(self, program: Program) -> RunResult:
        stack = []
        locals_ns = {}
        trace = []
        pc = 0
        steps = 0
        code = program.instructions

        while 0 <= pc < len(code):
            steps += 1
            if steps > MAX_STEPS:
                raise VMError(f"sandbox step limit exceeded ({MAX_STEPS})")
            op_id, operand = code[pc]
            name = ID_TO_NAME.get(op_id)
            if name is None:
                raise VMError(f"unknown opcode id 0x{op_id:02X} at pc={pc}")
            cur = pc
            pc += 1  # default: fall through; jumps overwrite

            if name == "NOP":
                pass
            elif name == "HALT":
                trace.append(self._step(cur, name, stack, locals_ns))
                return RunResult(None, stack, locals_ns, trace, halted=True)
            elif name == "LOAD_CONST":
                stack.append(operand)
            elif name == "LOAD_NAME":
                if operand not in self.builtins:
                    raise VMError(f"LOAD_NAME: unknown name '{operand}'")
                stack.append(self.builtins[operand])
            elif name == "LOAD_FAST":
                if operand not in locals_ns:
                    raise VMError(f"LOAD_FAST: undefined local '{operand}'")
                stack.append(locals_ns[operand])
            elif name == "STORE_FAST":
                locals_ns[operand] = self._pop(stack)
            elif name == "CALL_FUNCTION":
                argc = operand
                args = [self._pop(stack) for _ in range(argc)][::-1]
                fn = self._pop(stack)
                if not callable(fn):
                    raise VMError("CALL_FUNCTION: top of stack is not callable")
                stack.append(fn(*args))
            elif name == "RETURN_VALUE":
                value = self._pop(stack)
                trace.append(self._step(cur, name, stack, locals_ns))
                return RunResult(value, stack, locals_ns, trace, halted=False)
            elif name == "LOAD_ATTR":
                attr = operand
                obj = self._pop(stack)
                if isinstance(obj, dict):
                    if attr not in obj:
                        raise VMError(f"LOAD_ATTR: no key '{attr}'")
                    stack.append(obj[attr])
                else:
                    if attr.startswith("_"):
                        raise VMError("LOAD_ATTR: private attributes are blocked")
                    stack.append(getattr(obj, attr))
            elif name == "BUILD_MAP":
                pairs = operand
                items = [self._pop(stack) for _ in range(pairs * 2)][::-1]
                stack.append({items[i]: items[i + 1]
                              for i in range(0, len(items), 2)})
            elif name == "BUILD_LIST":
                items = [self._pop(stack) for _ in range(operand)][::-1]
                stack.append(items)
            elif name == "LIST_APPEND":
                item = self._pop(stack)
                lst = self._pop(stack)
                if not isinstance(lst, list):
                    raise VMError("LIST_APPEND: target is not a list")
                lst.append(item)  # spec stack effect: [list,item] -> [list]
                stack.append(lst)
            elif name == "MAP_SET":
                value = self._pop(stack)
                key = self._pop(stack)
                m = self._pop(stack)
                if not isinstance(m, dict):
                    raise VMError("MAP_SET: target is not a map")
                m[key] = value
                stack.append(m)
            elif name == "COMPARE_EQ":
                b = self._pop(stack); a = self._pop(stack)
                stack.append(a == b)  # spec: no type coercion games; plain eq
            elif name == "COMPARE_LT":
                b = self._pop(stack); a = self._pop(stack)
                a, b = self._numeric(name, a, b)
                stack.append(a < b)
            elif name == "COMPARE_GT":
                b = self._pop(stack); a = self._pop(stack)
                a, b = self._numeric(name, a, b)
                stack.append(a > b)
            elif name == "BINARY_ADD":
                b = self._pop(stack); a = self._pop(stack)
                a, b = self._numeric(name, a, b)
                stack.append(a + b)
            elif name == "BINARY_SUB":
                b = self._pop(stack); a = self._pop(stack)
                a, b = self._numeric(name, a, b)
                stack.append(a - b)
            elif name == "BINARY_MUL":
                b = self._pop(stack); a = self._pop(stack)
                a, b = self._numeric(name, a, b)
                stack.append(a * b)
            elif name == "BINARY_DIV":
                b = self._pop(stack); a = self._pop(stack)
                a, b = self._numeric(name, a, b)
                if b == 0:
                    raise VMError("BINARY_DIV: division by zero")
                stack.append(a / b)
            elif name == "JUMP":
                pc = operand
            elif name == "JUMP_IF_FALSE":
                cond = self._pop(stack)
                if not cond:
                    pc = operand
            elif name == "POP_TOP":
                self._pop(stack)
            elif name == "CHECK_POLICY":
                action = self._pop(stack)
                allowed = bool(self.policy_check(action)) if self.policy_check else False
                stack.append(allowed)
            elif name == "CHECK_IDENTITY":
                token = self._pop(stack)
                ok = bool(self.identity_check(token)) if self.identity_check else False
                stack.append(ok)
            elif name == "CHECK_SIGNATURE":
                msg = self._pop(stack)
                ok = bool(self.signature_check(msg)) if self.signature_check else False
                stack.append(ok)
            else:  # pragma: no cover - table and dispatch kept in sync
                raise VMError(f"unimplemented opcode {name}")

            trace.append(self._step(cur, name, stack, locals_ns))

        # Fell off the end without RETURN_VALUE / HALT.
        return RunResult(None, stack, locals_ns, trace, halted=True)

    @staticmethod
    def _step(pc, name, stack, locals_ns):
        return TraceStep(
            pc=pc, opcode=name,
            stack=[_safe_repr(v) for v in stack],
            locals={k: _safe_repr(v) for k, v in locals_ns.items()},
        )


def _safe_repr(value, limit=120):
    if callable(value):
        text = f"<fn {getattr(value, '__name__', 'builtin')}>"
    else:
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def result_to_dict(result: RunResult, program: Program) -> dict:
    """JSON-serialisable view of a run (used by AirBridge and the AirUI)."""
    return {
        "return_value": result.return_value,
        "final_stack": [_safe_repr(v) for v in result.stack],
        "locals": {k: _safe_repr(v) for k, v in result.locals.items()},
        "halted": result.halted,
        "listing": disassemble(program),
        "trace": [
            {"step": i + 1, "pc": s.pc, "opcode": s.opcode,
             "stack": s.stack, "locals": s.locals}
            for i, s in enumerate(result.trace)
        ],
    }
