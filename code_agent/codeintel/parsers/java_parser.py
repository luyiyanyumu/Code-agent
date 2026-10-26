"""Conservative Java declaration index, without pretending to resolve Java types.

Only declarations at file/type member scope are indexed. Names in executable code,
anonymous classes and local classes are deliberately omitted. Signatures use the
complete lexical parameter types; they are not compiler-resolved type identities.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

STRATEGY = "java-lexical-v1"
_MODIFIERS = frozenset("public protected private abstract static final transient volatile synchronized native strictfp default sealed non-sealed".split())
_TYPE_WORDS = frozenset({"class", "interface", "enum", "record"})
_CONTROL = frozenset("if for while switch catch return new throw assert synchronized this super".split())


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


def _identifier(value: str) -> bool:
    return bool(value) and value.replace("$", "_").isidentifier()


def _tokens(text: str) -> tuple[list[Token], list[tuple[int, int, str]]]:
    tokens: list[Token] = []
    comments: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        start, char = i, text[i]
        if char.isspace():
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
            comments.append((start, i, text[start:i]))
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            i = end + 2
            comments.append((start, i, text[start:i]))
            continue
        if text.startswith('"""', i):
            i += 3
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text.startswith('"""', i):
                    i += 3
                    break
                else:
                    i += 1
            else:
                raise ValueError("unterminated text block")
            tokens.append(Token("<literal>", start, i))
            continue
        if char in {'"', "'"}:
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == char:
                    i += 1
                    break
                elif text[i] in "\r\n":
                    raise ValueError("unterminated literal")
                else:
                    i += 1
            else:
                raise ValueError("unterminated literal")
            tokens.append(Token("<literal>", start, i))
            continue
        if text.startswith("\\u", i):
            raise ValueError("Java Unicode escapes require compiler preprocessing")
        if text.startswith("non-sealed", i) and (i + 10 >= len(text) or not (text[i + 10].isalnum() or text[i + 10] in "_$")):
            i += 10
        elif char.isalpha() or char in "_$" or (char.isidentifier() and not char.isdigit()):
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] in "_$" or ("a" + text[i]).isidentifier()):
                i += 1
        elif text.startswith("...", i):
            i += 3
        elif text.startswith(("->", "::"), i):
            i += 2
        else:
            i += 1
        tokens.append(Token(text[start:i], start, i))
    return tokens, comments


def _normal(tokens: list[Token]) -> str:
    out = ""
    previous = ""
    for token in tokens:
        value = token.value
        if out and (_identifier(previous) and _identifier(value) or previous == "?" and value in {"extends", "super"}):
            out += " "
        out += value
        previous = value
    return out


def _without_annotations(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        if tokens[i].value != "@":
            out.append(tokens[i])
            i += 1
            continue
        i += 1
        if i >= len(tokens) or not _identifier(tokens[i].value):
            return []
        i += 1
        while i + 1 < len(tokens) and tokens[i].value == "." and _identifier(tokens[i + 1].value):
            i += 2
        if i < len(tokens) and tokens[i].value == "(":
            depth = 1
            i += 1
            while i < len(tokens) and depth:
                depth += (tokens[i].value == "(") - (tokens[i].value == ")")
                i += 1
            if depth:
                return []
    return out


def _without_modifiers(tokens: list[Token]) -> list[Token]:
    return [t for t in _without_annotations(tokens) if t.value not in _MODIFIERS]


def _split(tokens: list[Token], delimiter: str = ",") -> list[list[Token]]:
    result: list[list[Token]] = []
    start = 0
    paren = bracket = angle = brace = 0
    for i, token in enumerate(tokens):
        value = token.value
        if value == delimiter and not (paren or bracket or angle or brace):
            result.append(tokens[start:i])
            start = i + 1
        paren += (value == "(") - (value == ")")
        bracket += (value == "[") - (value == "]")
        brace += (value == "{") - (value == "}")
        angle += (value == "<") - (value == ">" and angle > 0)
    result.append(tokens[start:])
    return result


def _valid_type(tokens: list[Token], *, allow_void: bool = False) -> bool:
    if not tokens or not _identifier(tokens[0].value):
        return False
    angle = 0
    for token in tokens:
        value = token.value
        if not (_identifier(value) or value in {".", "[", "]", "<", ">", ",", "?", "&", "..."}):
            return False
        if value in _CONTROL | _TYPE_WORDS or value == "void" and not (allow_void and len(tokens) == 1):
            return False
        angle += (value == "<") - (value == ">")
        if angle < 0:
            return False
    return angle == 0


def _parameter(tokens: list[Token]) -> dict[str, Any] | None:
    clean = _without_modifiers(tokens)
    if not clean or any(t.value in {"=", ";", "->", "<literal>"} for t in clean):
        return None
    end = len(clean)
    while end >= 2 and [t.value for t in clean[end - 2:end]] == ["[", "]"]:
        end -= 2
    if end < 2 or not _identifier(clean[end - 1].value) or clean[end - 1].value in _CONTROL:
        return None
    name = clean[end - 1]
    if name.value == "this":  # Receiver parameters have no ordinary variable declaration.
        return None
    type_tokens = clean[:end - 1] + clean[end:]
    if not _valid_type(type_tokens):
        return None
    return {"name": name.value, "type": _normal(type_tokens), "token": name}


def _javadoc(raw: str) -> tuple[str, dict[str, str], str]:
    if raw.startswith("/**"):
        raw = raw[3:-2]
    elif raw.startswith("//"):
        match = re.match(r"//\s*(?:作用|用途|功能|purpose)\s*[:：]\s*(.*)", raw, re.I)
        return (match.group(1).strip(), {}, "") if match else ("", {}, "")
    else:
        return "", {}, ""
    lines = [re.sub(r"^\s*\* ?", "", line).strip() for line in raw.splitlines()]
    main: list[str] = []
    params: dict[str, list[str]] = {}
    returns: list[str] = []
    active: list[str] | None = main
    for line in lines:
        if line.startswith("@param "):
            match = re.match(r"@param\s+([^\s]+)\s*(.*)", line)
            if match and not match.group(1).startswith("<"):
                active = params.setdefault(match.group(1), [])
                active.append(match.group(2))
            else:
                active = None
        elif line.startswith("@return"):
            active = returns
            active.append(line[7:].strip())
        elif line.startswith("@"):
            active = None
        elif active is not None:
            active.append(line)
    compact = lambda values: " ".join(v for v in values if v).strip()
    return compact(main), {k: compact(v) for k, v in params.items()}, compact(returns)


class _Parser:
    def __init__(self, rel: str, text: str):
        self.rel, self.text = rel, text
        self.tokens, self.comments = _tokens(text)
        self.comment_ends = [end for _, end, _ in self.comments]
        self.lines = [0] + [m.end() for m in re.finditer("\n", text)]
        self.astral_offsets = [i for i, char in enumerate(text) if ord(char) > 0xFFFF]
        self.pairs: dict[int, int] = {}
        stack: list[tuple[str, int]] = []
        for i, token in enumerate(self.tokens):
            value = token.value
            if value in {"(", "[", "{"}:
                stack.append((value, i))
            elif value in {")", "]", "}"}:
                expected = {")": "(", "]": "[", "}": "{"}[value]
                if not stack or stack[-1][0] != expected:
                    raise ValueError("unbalanced Java declaration delimiters")
                _, opening = stack.pop()
                self.pairs[opening] = i
        if stack:
            raise ValueError("unbalanced Java declaration delimiters")
        self.symbols: list[dict] = []
        self.package = ""
        self.imports: list[dict] = []

    def point(self, offset: int) -> dict[str, int]:
        line = bisect.bisect_right(self.lines, offset) - 1
        # Repeated hovers on generated, very long lines must not encode the
        # complete line prefix for every declaration (quadratic work).
        char = offset - self.lines[line] + bisect.bisect_left(self.astral_offsets, offset) - bisect.bisect_left(self.astral_offsets, self.lines[line])
        return {"line": line, "character": char}

    def range(self, start: int, end: int) -> dict:
        return {"start": self.point(start), "end": self.point(end)}

    def docs(self, start: int) -> tuple[str, dict[str, str], str]:
        i = bisect.bisect_right(self.comment_ends, start) - 1
        if i < 0:
            return "", {}, ""
        _, end, raw = self.comments[i]
        if self.text[end:start].strip():
            return "", {}, ""
        return _javadoc(raw)

    def documentation_start(self, start: int) -> int:
        i = bisect.bisect_right(self.comment_ends, start) - 1
        if i >= 0:
            comment_start, end, raw = self.comments[i]
            if not self.text[end:start].strip() and (raw.startswith("/**") or _javadoc(raw)[0]):
                return comment_start
        return start

    def bases(self, start: int, stop: int) -> list[str]:
        gathered: list[Token] = []
        collecting = False
        angle = 0
        i = start
        while i < stop:
            token = self.tokens[i]
            value = token.value
            if value == "(" and not angle:
                i = self.pairs[i] + 1
                continue
            if not angle and value in {"extends", "implements", "permits"}:
                if gathered:
                    gathered.append(Token(",", token.start, token.end))
                collecting = value != "permits"
            elif collecting:
                gathered.append(token)
            angle += (value == "<") - (value == ">" and angle > 0)
            i += 1
        cleaned = [_without_annotations(part) for part in _split(gathered)]
        return [_normal(part) for part in cleaned if part and _valid_type(part)]

    def add(self, kind: str, token: Token, qname: str, parent: str, start: int, end: int, *, doc: str = "", params: list[str] | None = None, returns: str = "", extra: dict | None = None) -> dict:
        metadata = {
            "java_kind": kind, "lexical_identity": True,
            "selection_range": self.range(token.start, token.end),
            "declaration_range": self.range(start, end),
            # Include attached author documentation, including @throws/other
            # tags, so changing any business evidence invalidates its cache.
            "source_range": {"start": self.documentation_start(start), "end": end}, "owner": parent,
        }
        metadata.update(extra or {})
        symbol = {
            "id": f"sym:{self.rel}#{qname}", "name": token.value, "qname": qname,
            "kind": kind, "parent": parent, "line": self.point(start)["line"] + 1,
            "end_line": self.point(max(start, end - 1))["line"] + 1,
            "signature": re.sub(r"\s+", " ", self.text[start:token.end]).strip(),
            "doc": doc, "params": params or [], "returns": returns,
            "decorators": [], "bases": [], "complexity": 0,
            "confidence": "medium", "extra": metadata,
        }
        self.symbols.append(symbol)
        return symbol

    def header_end(self, start: int, stop: int) -> int | None:
        i = start
        while i < stop:
            value = self.tokens[i].value
            if value in {"{", ";"}:
                return i
            if value in {"(", "["}:
                i = self.pairs[i] + 1
            else:
                i += 1
        return None

    def type_header(self, start: int, end: int) -> tuple[int, str] | None:
        clean = _without_modifiers(self.tokens[start:end])
        # @interface is a declaration rather than an annotation.
        raw = self.tokens[start:end]
        for j, token in enumerate(raw[:-1]):
            if token.value == "interface" and j > 0 and raw[j - 1].value == "@" and _identifier(raw[j + 1].value):
                return start + j + 1, "annotation"
        if len(clean) >= 2 and clean[0].value in _TYPE_WORDS and _identifier(clean[1].value):
            return self.tokens.index(clean[1], start, end), clean[0].value
        return None

    def parse_type(self, start: int, opening: int, info: tuple[int, str], owner: dict | None) -> int:
        closing = self.pairs[opening]
        name_index, kind = info
        name = self.tokens[name_index]
        qname = f"{owner['qname']}.{name.value}" if owner else ".".join(filter(None, [self.package, name.value]))
        doc, param_docs, _ = self.docs(self.tokens[start].start)
        symbol = self.add(kind, name, qname, owner["id"] if owner else "", self.tokens[start].start, self.tokens[closing].end, doc=doc)
        symbol["signature"] = re.sub(r"\s+", " ", self.text[self.tokens[start].start:self.tokens[opening].start]).strip()
        symbol["bases"] = self.bases(name_index + 1, opening)
        symbol["extra"]["type_signature"] = _normal(self.tokens[name_index:opening])
        components: list[dict] = []
        if kind == "record":
            paren = next((i for i in range(name_index + 1, opening) if self.tokens[i].value == "("), None)
            if paren is not None:
                for index, part in enumerate(_split(self.tokens[paren + 1:self.pairs[paren]])):
                    component = _parameter(part)
                    if component:
                        components.append(component)
                        token = component["token"]
                        self.add("record_component", token, qname + "." + token.value, symbol["id"], part[0].start, part[-1].end, doc=param_docs.get(token.value, ""), returns=component["type"], extra={"type": component["type"], "parameter_index": index})
        symbol["extra"]["record_components"] = [{"name": p["name"], "type": p["type"]} for p in components]
        i = opening + 1
        if kind == "enum":
            i = self.enum_members(i, closing, symbol)
        while i < closing:
            if self.tokens[i].value == ";":
                i += 1
                continue
            delimiter = self.header_end(i, closing)
            if delimiter is None:
                break
            header = self.type_header(i, delimiter)
            if header and self.tokens[delimiter].value == "{":
                i = self.parse_type(i, delimiter, header, symbol)
                continue
            method = self.method(i, delimiter, symbol, components)
            if method:
                i = self.pairs[delimiter] + 1 if self.tokens[delimiter].value == "{" else delimiter + 1
                continue
            # Initializer/anonymous/lambda bodies are not declaration scopes.
            if self.tokens[delimiter].value == "{":
                clean = _without_modifiers(self.tokens[i:delimiter])
                if not clean:
                    i = self.pairs[delimiter] + 1
                    continue
                end = self.pairs[delimiter] + 1
                while end < closing and self.tokens[end].value != ";":
                    if self.tokens[end].value in {"(", "[", "{"}:
                        end = self.pairs[end] + 1
                    else:
                        end += 1
                if end >= closing:
                    break
            else:
                end = delimiter
            self.fields(i, end, symbol)
            i = end + 1
        return closing + 1

    def enum_members(self, start: int, stop: int, owner: dict) -> int:
        i = start
        while i < stop:
            end = i
            while end < stop and self.tokens[end].value not in {",", ";"}:
                if self.tokens[end].value in {"(", "[", "{"}:
                    end = self.pairs[end] + 1
                else:
                    end += 1
            clean = _without_annotations(self.tokens[i:end])
            if not clean or not _identifier(clean[0].value):
                return i
            if len(clean) > 1 and clean[1].value not in {"(", "{"}:
                return i
            token = clean[0]
            doc, _, _ = self.docs(self.tokens[i].start)
            self.add("enum_member", token, owner["qname"] + "." + token.value, owner["id"], self.tokens[i].start, self.tokens[end - 1].end, doc=doc, returns=owner["qname"])
            if end >= stop:
                return stop
            if self.tokens[end].value == ";":
                return end + 1
            i = end + 1
        return i

    def method(self, start: int, delimiter: int, owner: dict, components: list[dict]) -> bool:
        clean = _without_modifiers(self.tokens[start:delimiter])
        if not clean or any(t.value in {"=", "->"} for t in clean):
            return False
        # Remove a method type-parameter declaration, preserving the return type.
        if clean[0].value == "<":
            depth, j = 1, 1
            while j < len(clean) and depth:
                depth += (clean[j].value == "<") - (clean[j].value == ">")
                j += 1
            clean = clean[j:] if not depth else []
        paren = next((j for j, t in enumerate(clean) if t.value == "("), None)
        compact = paren is None and owner["kind"] == "record" and len(clean) == 1 and clean[0].value == owner["name"] and self.tokens[delimiter].value == "{"
        if compact:
            name, parameters, returns = clean[0], components, ""
        elif paren is not None and paren >= 1:
            name = clean[paren - 1]
            if not _identifier(name.value) or name.value in _CONTROL or (paren >= 2 and clean[paren - 2].value == "."):
                return False
            is_constructor = name.value == owner["name"] and paren == 1
            prefix = clean[:paren - 1]
            if not is_constructor and not _valid_type(prefix, allow_void=True):
                return False
            raw_paren = self.tokens.index(clean[paren], start, delimiter)
            close = self.pairs[raw_paren]
            pieces = _split(self.tokens[raw_paren + 1:close])
            parameters = []
            for piece in pieces:
                if not piece:
                    continue
                parameter = _parameter(piece)
                if parameter is None:
                    return False
                parameters.append(parameter)
            # Only throws, array return suffixes and annotation defaults may follow.
            suffix = _without_annotations(self.tokens[close + 1:delimiter])
            if suffix and suffix[0].value not in {"throws", "default", "["}:
                return False
            returns = "" if is_constructor else _normal(prefix)
            if suffix and suffix[0].value == "[":
                k = 0
                while k + 1 < len(suffix) and suffix[k].value == "[" and suffix[k + 1].value == "]":
                    returns += "[]"
                    k += 2
        else:
            return False
        kind = "constructor" if compact or name.value == owner["name"] and not returns else "method"
        type_signature = "(" + ",".join(p["type"].replace("...", "[]") for p in parameters) + ")"
        qname = owner["qname"] + "." + name.value + type_signature
        doc, param_docs, return_doc = self.docs(self.tokens[start].start)
        end = self.tokens[self.pairs[delimiter]].end if self.tokens[delimiter].value == "{" else self.tokens[delimiter].end
        if not compact and self.tokens[delimiter].value == "{" and suffix and suffix[0].value == "default":
            after_default = self.pairs[delimiter] + 1
            if after_default < len(self.tokens) and self.tokens[after_default].value == ";":
                end = self.tokens[after_default].end
        symbol = self.add(kind, name, qname, owner["id"], self.tokens[start].start, end, doc=doc, params=[p["name"] for p in parameters], returns=returns, extra={"type_signature": type_signature, "return_doc": return_doc, "parameters": [{"name": p["name"], "type": p["type"], "doc": param_docs.get(p["name"], ""), "index": j} for j, p in enumerate(parameters)]})
        symbol["signature"] = re.sub(r"\s+", " ", self.text[self.tokens[start].start:self.tokens[delimiter].start]).strip()
        if not compact:
            for j, parameter in enumerate(parameters):
                token = parameter["token"]
                self.add("parameter", token, qname + f"@param:{j}:" + token.value, symbol["id"], token.start, token.end, doc=param_docs.get(token.value, ""), returns=parameter["type"], extra={"type": parameter["type"], "parameter_index": j, "business_context_range": dict(symbol["extra"]["source_range"])})
        return True

    def fields(self, start: int, semicolon: int, owner: dict) -> None:
        annotated = _without_annotations(self.tokens[start:semicolon])
        clean = [t for t in annotated if t.value not in _MODIFIERS]
        if not clean or clean[0].value in _CONTROL or clean[0].value in _TYPE_WORDS:
            return
        parts = _split(clean)
        declarations: list[tuple[dict, list[Token]]] = []
        base_type: list[Token] = []
        for j, part in enumerate(parts):
            equal = next((i for i, t in enumerate(part) if t.value == "="), len(part))
            head = part[:equal]
            parameter = _parameter(head) if j == 0 else _parameter(base_type + head)
            if parameter is None:
                return
            if j == 0:
                # Array dimensions after the first name do not belong to subsequent names.
                name_at = head.index(parameter["token"])
                base_type = head[:name_at]
            declarations.append((parameter, part))
        doc, _, _ = self.docs(self.tokens[start].start)
        declaration_end = next((i for i, t in enumerate(annotated) if t.value == "="), len(annotated))
        modifiers = [t.value for t in annotated[:declaration_end] if t.value in _MODIFIERS]
        for parameter, part in declarations:
            token = parameter["token"]
            self.add("field", token, owner["qname"] + "." + token.value, owner["id"], self.tokens[start].start, self.tokens[semicolon].end, doc=doc, returns=parameter["type"], extra={"type": parameter["type"], "modifiers": modifiers})

    def run(self) -> dict:
        i = 0
        while i < len(self.tokens):
            token = self.tokens[i]
            if token.value in {"package", "import"}:
                end = next((j for j in range(i + 1, len(self.tokens)) if self.tokens[j].value == ";"), None)
                if end is None:
                    break
                raw = self.tokens[i + 1:end]
                if token.value == "package":
                    self.package = _normal(raw)
                else:
                    static = bool(raw and raw[0].value == "static")
                    target = _normal(raw[1:] if static else raw)
                    wildcard = target.endswith(".*")
                    module = target.rsplit(".", 1)[0] if static else target
                    self.imports.append({"module": module, "level": 0, "names": [target.rsplit(".", 1)[-1]] if static and not wildcard else [], "raw": self.text[token.start:self.tokens[end].end], "static": static, "target": target})
                i = end + 1
                continue
            delimiter = self.header_end(i, len(self.tokens))
            if delimiter is None:
                break
            info = self.type_header(i, delimiter)
            if info and self.tokens[delimiter].value == "{":
                i = self.parse_type(i, delimiter, info, None)
            else:
                i = self.pairs[delimiter] + 1 if self.tokens[delimiter].value == "{" else delimiter + 1
        # Broken/incomplete duplicate declarations cannot be reliably identified.
        counts = Counter(s["id"] for s in self.symbols)
        rejected = {sid for sid, n in counts.items() if n > 1}
        while True:
            descendants = {s["id"] for s in self.symbols if s["parent"] in rejected}
            if descendants <= rejected:
                break
            rejected |= descendants
        symbols = [s for s in self.symbols if s["id"] not in rejected]
        # Shared with store and transient editor resolution, so a live buffer's
        # hashes have exactly the same meaning as its persisted index rows.
        from code_agent.codeintel.store import symbol_hashes
        context = {"package": self.package, "imports": sorted((i["static"], i["target"]) for i in self.imports)}
        context_hash = hashlib.sha1(json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        for symbol in symbols:
            symbol["extra"]["resolution_context_hash"] = context_hash
            symbol["content_hash"], symbol["body_hash"] = symbol_hashes(symbol, self.text)
        return {"symbols": symbols, "imports": self.imports, "entrypoint": any(s["name"] == "main" and s["kind"] == "method" and s["returns"] == "void" and s["extra"].get("type_signature") == "(String[])" for s in symbols), "error": "", "confidence": "medium", "strategy": STRATEGY}


def parse(rel_path: str, text: str) -> dict[str, Any]:
    return _Parser(rel_path, text).run()
