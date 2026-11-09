"""
AirUI compiler — Increment 2.

Compiles the spec's declarative AirUI format (AirUI/main.airui inside an
Airlet package) to AirCore bytecode, per the spec: "AirUI compiles to
AirCore bytecode and binds to AirBridge.state (e.g.
Text(AirBridge.state.output))".

Format (line-based; ';' and '#' start comments):

    airlet <Name> <version>

    view:
      column:
        text "literal text"
        text bind state.output
        button "Label" action <handler>

    handler <name>:
      <statements>

View elements nest by indentation (2 spaces per level); `column:` is the
only container in this increment. A `text bind state.<name>` element
compiles to a LOAD_NAME of the builtin "state.<name>", so the render
program resolves the binding against whatever AirBridge.state value the
runner supplies as that builtin.

Handler statements (one per line):
    name = expr            assignment (locals)
    return expr
    if expr: ... [else: ...] end
    expr                   evaluated and discarded

Handler expressions: int/float/string literals, true/false/null, names
(locals, or the builtins diagnostics()/execute()), calls name(args),
policy("action") -> CHECK_POLICY, map literals {"k": expr, ...}, list
literals [expr, ...], parentheses, unary -, and operators
+ - * /  >  <  ==  (comparison lowest precedence, non-chained).

Each handler compiles to its own Program; the view compiles to a
"__view__" Program that BUILD_MAP/BUILD_LISTs the view tree (with live
state bindings) and RETURN_VALUEs it. compile_airui() also returns the
emitted assembly text per program, which is handy for inspection and is
exactly what the assembler in aircore_vm.py consumes.

Errors raise AiruiError with the .airui line number where known.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aircore_vm import Program, assemble

BUILTINS = {"diagnostics", "execute"}


class AiruiError(Exception):
    pass


# ---------------------------------------------------------------------------
# Expression tokenizer / parser
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<num>\d+(?:\.\d+)?)
    | (?P<str>"(?:[^"\\]|\\.)*")
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>==|[-+*/><(){}\[\]:,=])
    )""", re.VERBOSE)


def _tokenize(text: str, lineno: int) -> list:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise AiruiError(f"line {lineno}: cannot tokenize near {text[pos:pos+12]!r}")
        pos = m.end()
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
    tokens.append(("eof", ""))
    return tokens


class _ExprParser:
    """Recursive-descent parser producing a tiny AST of tuples."""

    def __init__(self, text: str, lineno: int):
        self.tokens = _tokenize(text.strip(), lineno)
        self.i = 0
        self.lineno = lineno

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect_op(self, op):
        kind, val = self.next()
        if (kind, val) != ("op", op):
            raise AiruiError(f"line {self.lineno}: expected '{op}', got '{val}'")

    def parse(self):
        node = self.comparison()
        if self.peek()[0] != "eof":
            raise AiruiError(f"line {self.lineno}: trailing tokens in expression")
        return node

    def comparison(self):
        node = self.additive()
        kind, val = self.peek()
        if kind == "op" and val in (">", "<", "=="):
            self.next()
            rhs = self.additive()
            node = ("cmp", val, node, rhs)
        return node

    def additive(self):
        node = self.term()
        while True:
            kind, val = self.peek()
            if kind == "op" and val in ("+", "-"):
                self.next()
                node = ("bin", val, node, self.term())
            else:
                return node

    def term(self):
        node = self.unary()
        while True:
            kind, val = self.peek()
            if kind == "op" and val in ("*", "/"):
                self.next()
                node = ("bin", val, node, self.unary())
            else:
                return node

    def unary(self):
        kind, val = self.peek()
        if (kind, val) == ("op", "-"):
            self.next()
            return ("neg", self.unary())
        return self.atom()

    def atom(self):
        kind, val = self.next()
        if kind == "num":
            return ("lit", float(val) if "." in val else int(val))
        if kind == "str":
            import json
            return ("lit", json.loads(val))
        if kind == "name":
            if val in ("true", "false"):
                return ("lit", val == "true")
            if val == "null":
                return ("lit", None)
            if self.peek() == ("op", "("):
                self.next()
                args = []
                if self.peek() != ("op", ")"):
                    args.append(self.comparison())
                    while self.peek() == ("op", ","):
                        self.next()
                        args.append(self.comparison())
                self.expect_op(")")
                return ("call", val, args)
            return ("name", val)
        if (kind, val) == ("op", "("):
            node = self.comparison()
            self.expect_op(")")
            return node
        if (kind, val) == ("op", "{"):
            pairs = []
            if self.peek() != ("op", "}"):
                while True:
                    kkind, kval = self.next()
                    if kkind != "str":
                        raise AiruiError(f"line {self.lineno}: map keys must be strings")
                    import json
                    key = json.loads(kval)
                    self.expect_op(":")
                    pairs.append((key, self.comparison()))
                    if self.peek() == ("op", ","):
                        self.next()
                        continue
                    break
            self.expect_op("}")
            return ("map", pairs)
        if (kind, val) == ("op", "["):
            items = []
            if self.peek() != ("op", "]"):
                items.append(self.comparison())
                while self.peek() == ("op", ","):
                    self.next()
                    items.append(self.comparison())
            self.expect_op("]")
            return ("list", items)
        raise AiruiError(f"line {self.lineno}: unexpected token '{val}'")


# ---------------------------------------------------------------------------
# Code generation: AST -> AirCore assembly text
# ---------------------------------------------------------------------------

_BIN_OPS = {"+": "BINARY_ADD", "-": "BINARY_SUB", "*": "BINARY_MUL", "/": "BINARY_DIV"}
_CMP_OPS = {">": "COMPARE_GT", "<": "COMPARE_LT", "==": "COMPARE_EQ"}


class _Codegen:
    def __init__(self):
        self.lines = []
        self.locals = set()
        self._label_counter = 0

    def emit(self, text):
        self.lines.append(text)

    def new_label(self, hint):
        self._label_counter += 1
        return f"{hint}_{self._label_counter}"

    def expr(self, node):
        kind = node[0]
        if kind == "lit":
            import json
            self.emit(f"LOAD_CONST {json.dumps(node[1])}")
        elif kind == "name":
            name = node[1]
            if name in self.locals:
                self.emit(f"LOAD_FAST {name}")
            elif name in BUILTINS:
                self.emit(f"LOAD_NAME {name}")
            else:
                raise AiruiError(f"undefined name '{name}'")
        elif kind == "call":
            _, fname, args = node
            if fname == "policy":
                if len(args) != 1:
                    raise AiruiError("policy() takes exactly one argument")
                self.expr(args[0])
                self.emit("CHECK_POLICY")
            elif fname in BUILTINS:
                self.emit(f"LOAD_NAME {fname}")
                for a in args:
                    self.expr(a)
                self.emit(f"CALL_FUNCTION {len(args)}")
            else:
                raise AiruiError(f"unknown function '{fname}'")
        elif kind == "bin":
            _, op, lhs, rhs = node
            self.expr(lhs)
            self.expr(rhs)
            self.emit(_BIN_OPS[op])
        elif kind == "cmp":
            _, op, lhs, rhs = node
            self.expr(lhs)
            self.expr(rhs)
            self.emit(_CMP_OPS[op])
        elif kind == "neg":
            self.emit("LOAD_CONST 0")
            self.expr(node[1])
            self.emit("BINARY_SUB")
        elif kind == "map":
            import json
            for key, val in node[1]:
                self.emit(f"LOAD_CONST {json.dumps(key)}")
                self.expr(val)
            self.emit(f"BUILD_MAP {len(node[1])}")
        elif kind == "list":
            for item in node[1]:
                self.expr(item)
            self.emit(f"BUILD_LIST {len(node[1])}")
        else:  # pragma: no cover
            raise AiruiError(f"internal: unknown AST node {kind}")


# ---------------------------------------------------------------------------
# .airui document parsing
# ---------------------------------------------------------------------------

@dataclass
class CompiledAirlet:
    name: str
    version: str
    view_program: Program
    handlers: dict          # handler name -> Program
    airasm: dict            # program name -> emitted assembly text ("__view__" + handlers)


def _parse_view(lines, i):
    """Parse the view: block starting at index i (first line after 'view:').
    Returns (root_node, next_index). Nodes are tuples:
    ('column', [children]) | ('text', value) | ('bind', 'state.x') |
    ('button', label, action)."""
    items = []  # (indent, node)
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith((";", "#")):
            i += 1
            continue
        if not raw.startswith(" "):
            break  # dedent to top level: view block is over
        indent = (len(raw) - len(raw.lstrip(" "))) // 2
        if stripped == "column:":
            items.append((indent, ("column", [])))
        elif stripped.startswith("text "):
            rest = stripped[5:].strip()
            if rest.startswith("bind "):
                items.append((indent, ("bind", rest[5:].strip())))
            else:
                import json
                try:
                    value = json.loads(rest)
                except json.JSONDecodeError:
                    value = rest
                items.append((indent, ("text", value)))
        elif stripped.startswith("button "):
            m = re.match(r'button\s+("(?:[^"\\]|\\.)*")\s+action\s+([A-Za-z_][A-Za-z0-9_]*)',
                         stripped)
            if not m:
                raise AiruiError(f"view: malformed button line: {stripped!r}")
            import json
            items.append((indent, ("button", json.loads(m.group(1)), m.group(2))))
        else:
            raise AiruiError(f"view: unknown element: {stripped!r}")
        i += 1

    # Build the nesting tree from indentation.
    root = ("column", [])
    stack = [(-1, root)]  # (indent, column node)
    for indent, node in items:
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise AiruiError("view: bad indentation")
        stack[-1][1][1].append(node)
        if node[0] == "column":
            stack.append((indent, node))
    return root, i


def _emit_view_node(gen: _Codegen, node):
    import json
    kind = node[0]
    if kind == "column":
        gen.emit('LOAD_CONST "type"')
        gen.emit('LOAD_CONST "column"')
        gen.emit('LOAD_CONST "children"')
        for child in node[1]:
            _emit_view_node(gen, child)
        gen.emit(f"BUILD_LIST {len(node[1])}")
        gen.emit("BUILD_MAP 2")
    elif kind == "text":
        gen.emit('LOAD_CONST "type"')
        gen.emit('LOAD_CONST "text"')
        gen.emit('LOAD_CONST "value"')
        gen.emit(f"LOAD_CONST {json.dumps(node[1])}")
        gen.emit("BUILD_MAP 2")
    elif kind == "bind":
        gen.emit('LOAD_CONST "type"')
        gen.emit('LOAD_CONST "text"')
        gen.emit('LOAD_CONST "bind"')
        gen.emit(f"LOAD_CONST {json.dumps(node[1])}")
        gen.emit('LOAD_CONST "value"')
        gen.emit(f"LOAD_NAME {node[1]}")   # resolves AirBridge.state binding
        gen.emit("BUILD_MAP 3")
    elif kind == "button":
        gen.emit('LOAD_CONST "type"')
        gen.emit('LOAD_CONST "button"')
        gen.emit('LOAD_CONST "label"')
        gen.emit(f"LOAD_CONST {json.dumps(node[1])}")
        gen.emit('LOAD_CONST "action"')
        gen.emit(f"LOAD_CONST {json.dumps(node[2])}")
        gen.emit("BUILD_MAP 3")
    else:  # pragma: no cover
        raise AiruiError(f"internal: unknown view node {kind}")


def _compile_handler(name, stmt_lines):
    """stmt_lines: list of (lineno, text). Returns emitted assembly text."""
    gen = _Codegen()
    i = 0

    def compile_block(i, stop_words):
        while i < len(stmt_lines):
            lineno, text = stmt_lines[i]
            head = text.split(None, 1)[0].rstrip(":")
            if text.rstrip(":") in stop_words or head in stop_words:
                return i
            if text.startswith("if ") and text.endswith(":"):
                cond = _ExprParser(text[3:-1], lineno).parse()
                gen.expr(cond)
                else_label = gen.new_label("else")
                end_label = gen.new_label("endif")
                gen.emit(f"JUMP_IF_FALSE {else_label}")
                i = compile_block(i + 1, {"else", "end"})
                if i >= len(stmt_lines):
                    raise AiruiError(f"line {lineno}: if without end")
                if stmt_lines[i][1].rstrip(":") == "else":
                    gen.emit(f"JUMP {end_label}")
                    gen.emit(f"{else_label}:")
                    i = compile_block(i + 1, {"end"})
                    if i >= len(stmt_lines):
                        raise AiruiError(f"line {lineno}: else without end")
                else:
                    gen.emit(f"{else_label}:")
                gen.emit(f"{end_label}:")
                i += 1  # consume 'end'
                continue
            if text.startswith("return "):
                gen.expr(_ExprParser(text[7:], lineno).parse())
                gen.emit("RETURN_VALUE")
                i += 1
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", text)
            if m:
                gen.expr(_ExprParser(m.group(2), lineno).parse())
                gen.locals.add(m.group(1))
                gen.emit(f"STORE_FAST {m.group(1)}")
                i += 1
                continue
            # bare expression statement
            gen.expr(_ExprParser(text, lineno).parse())
            gen.emit("POP_TOP")
            i += 1
        return i

    compile_block(0, set())
    gen.emit("LOAD_CONST null")
    gen.emit("RETURN_VALUE")
    return "\n".join(gen.lines) + "\n"


def compile_airui(source: str) -> CompiledAirlet:
    """Compile .airui source text to AirCore Programs."""
    lines = source.splitlines()
    name = version = None
    view_root = None
    handlers_src = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        i += 1
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("airlet "):
            parts = stripped.split()
            name = parts[1]
            version = parts[2] if len(parts) > 2 else "0"
            continue
        if stripped == "view:":
            view_root, i = _parse_view(lines, i)
            continue
        m = re.match(r"^handler\s+([A-Za-z_][A-Za-z0-9_]*):$", stripped)
        if m:
            hname = m.group(1)
            body = []
            while i < len(lines):
                raw2 = lines[i]
                s2 = raw2.strip()
                if s2 and not raw2.startswith(" ") and (
                        s2 == "view:" or s2.startswith("handler ")):
                    break
                if s2 and not s2.startswith((";", "#")):
                    body.append((i + 1, s2))
                i += 1
            handlers_src[hname] = body
            continue
        raise AiruiError(f"line {i}: unexpected top-level line: {stripped!r}")
    if name is None:
        raise AiruiError("missing 'airlet <Name> <version>' header")
    if view_root is None:
        raise AiruiError("missing 'view:' block")
    # A view with a single top-level column uses that column as the root
    # (the parser's synthetic root only wraps genuinely mixed content).
    if len(view_root[1]) == 1 and view_root[1][0][0] == "column":
        view_root = view_root[1][0]

    gen = _Codegen()
    _emit_view_node(gen, view_root)
    gen.emit("RETURN_VALUE")
    view_asm = "\n".join(gen.lines) + "\n"

    airasm = {"__view__": view_asm}
    handlers = {}
    for hname, body in handlers_src.items():
        asm = _compile_handler(hname, body)
        airasm[hname] = asm
        handlers[hname] = assemble(asm)
    return CompiledAirlet(name=name, version=version,
                          view_program=assemble(view_asm),
                          handlers=handlers, airasm=airasm)


if __name__ == "__main__":  # CLI: python3 airui.py file.airui
    import sys
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        compiled = compile_airui(fh.read())
    print(f"Airlet {compiled.name} v{compiled.version}")
    for pname, asm in compiled.airasm.items():
        print(f"\n--- {pname} (assembly) ---\n{asm}")
