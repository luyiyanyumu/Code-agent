"""Chinese display projections for explicitly documented LSP prose fields.

Protocol names, code, edits and opaque server state are never translation
inputs. Originals travel with the display object so resolve/action requests
can restore the exact server-facing text before being sent back.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import Any


_ORIGINAL = "_code_agent_original"
_ADDED = "__added__"
_OPAQUE = frozenset({
    "data", "arguments", "edit", "edits", "textEdit", "textEdits",
    "additionalTextEdits", "changes", "documentChanges", "newText",
})
_RESTORABLE = frozenset({
    "contents", "documentation", "detail", "message", "title", "reason",
    "tooltip", "label", "documentation_translation",
})
_IDENTIFIER = r"[A-Za-z_$][\w$]*"
_QUOTED_NAME = re.compile(r"(['\"`])(" + _IDENTIFIER + r")\1")
_CALL_NAME = re.compile(r"\b(" + _IDENTIFIER + r")\s*\(")
_WORDS = re.compile(r"\b[A-Za-z]{2,}\b")
_CODE_START = re.compile(
    r"^\s*(?:(?:public|protected|private|static|final|abstract|native|synchronized|"
    r"readonly|export|declare|async|unsafe|unsigned|signed|const|volatile)\s+|"
    r"(?:class|interface|enum|record|struct|trait|impl|fn|def|function|type|namespace|"
    r"package|import|using|typedef)\b)", re.I,
)
_PROSE_START = re.compile(
    r"^\s*(?:returns?|creates?|represents?|gets?|sets?|provides?|contains?|computes?|"
    r"converts?|checks?|determines?|indicates?|specifies?|deprecated|the|an?|this|"
    r"use|used|see|whether|if)\b", re.I,
)


def _metadata(owner: dict) -> dict:
    value = owner.get(_ORIGINAL)
    if not isinstance(value, dict):
        value = {}
        owner[_ORIGINAL] = value
    return value


def _remember(owner: dict, key: str, original: Any) -> None:
    _metadata(owner).setdefault(key, copy.deepcopy(original))


def restore_protocol_text(payload: Any) -> Any:
    """Deep-copy a display payload, restoring prose and removing our metadata.

    Opaque payloads are copied verbatim, including any server-owned keys that
    happen to have the same spelling as our display metadata.
    """
    def restore(value: Any) -> Any:
        if isinstance(value, list):
            return [restore(item) for item in value]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        originals = value.get(_ORIGINAL)
        output = {
            key: copy.deepcopy(item) if key in _OPAQUE else restore(item)
            for key, item in value.items() if key != _ORIGINAL
        }
        if isinstance(originals, dict):
            for key, original in originals.items():
                # Metadata cannot overwrite executable protocol fields.
                if key in _RESTORABLE:
                    output[key] = copy.deepcopy(original)
            added = originals.get(_ADDED)
            if isinstance(added, list):
                for key in added:
                    if isinstance(key, str) and key in _RESTORABLE and key not in originals:
                        output.pop(key, None)
        return output

    return restore(payload)


def _prose_detail(value: Any) -> bool:
    """Only opt in clear explanatory detail; type/signature detail is code."""
    if not isinstance(value, str) or not value.strip():
        return False
    if _CODE_START.search(value) or re.search(r"[`{};]|->|=>|::", value):
        return False
    if re.search(r"\b[\w$]+\s*\(|[<>\[\]]|\b[\w$]+\s*:\s*[\w$]", value):
        return False
    words = _WORDS.findall(value)
    if len(words) < 2:
        return False
    return bool(_PROSE_START.search(value) or len([word for word in words if word.islower()]) >= 3)


class _Fields:
    def __init__(self) -> None:
        self.slots: list[tuple[dict, str, Any]] = []
        self._seen: set[tuple[int, str]] = set()

    def add(self, owner: Any, key: str, *, contents: bool = False, detail: bool = False) -> None:
        if not isinstance(owner, dict) or key not in owner:
            return
        if isinstance(owner.get(_ORIGINAL), dict) and key in owner[_ORIGINAL]:
            return  # Already localized; retain its original receipt.
        value = owner[key]
        valid = isinstance(value, str) and bool(value.strip())
        if contents and isinstance(value, dict):
            valid = ("language" not in value and value.get("kind") in {"plaintext", "markdown"}
                     and isinstance(value.get("value"), str) and bool(value["value"].strip()))
        if not valid or (detail and not _prose_detail(value)):
            return
        identity = (id(owner), key)
        if identity not in self._seen:
            self._seen.add(identity)
            self.slots.append((owner, key, copy.deepcopy(value)))

    async def translate(self, translator: Any, *, scope: str) -> None:
        # Keep ordinary messages in one request. Very large completion lists
        # are split by serialized input size, rather than one call per item.
        batches: list[list[tuple[dict, str, Any]]] = []
        batch: list[tuple[dict, str, Any]] = []
        size = 2
        for slot in self.slots:
            cost = len(json.dumps(slot[2], ensure_ascii=False)) + 2
            if batch and (size + cost > 24_000 or len(batch) >= 128):
                batches.append(batch)
                batch, size = [], 2
            batch.append(slot)
            size += cost
        if batch:
            batches.append(batch)
        for batch in batches:
            values = [copy.deepcopy(slot[2]) for slot in batch]
            identifiers: set[str] = set()
            for value in values:
                text = value if isinstance(value, str) else value["value"]
                identifiers.update(match[2] for match in _QUOTED_NAME.finditer(text))
                identifiers.update(_CALL_NAME.findall(text))
            try:
                translated = await translator.translate_contents(
                    values, scope=scope, identifiers=identifiers, single_words=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if not isinstance(translated, list) or len(translated) != len(batch):
                continue
            for (owner, key, original), value in zip(batch, translated):
                if isinstance(original, str):
                    if not isinstance(value, str) or not value.strip():
                        continue
                    replacement = value
                else:
                    if (not isinstance(value, dict) or value.get("kind") != original["kind"]
                            or not isinstance(value.get("value"), str) or not value["value"].strip()):
                        continue
                    # A translation may only replace the document's prose,
                    # never other fields returned by a model/adapter.
                    replacement = {**original, "value": value["value"]}
                if replacement != original:
                    _remember(owner, key, original)
                    owner[key] = replacement


def _each(value: Any) -> list:
    return value if isinstance(value, list) else []


def _diagnostics(fields: _Fields, values: Any) -> None:
    for diagnostic in _each(values):
        if not isinstance(diagnostic, dict):
            continue
        fields.add(diagnostic, "message")
        for related in _each(diagnostic.get("relatedInformation")):
            fields.add(related, "message")


def _command(fields: _Fields, command: Any) -> None:
    fields.add(command, "title")


def _action(fields: _Fields, action: Any) -> None:
    if not isinstance(action, dict):
        return
    fields.add(action, "title")
    fields.add(action.get("disabled"), "reason")
    _command(fields, action.get("command"))
    _diagnostics(fields, action.get("diagnostics"))


def _symbols(fields: _Fields, symbols: Any) -> None:
    for symbol in _each(symbols):
        if not isinstance(symbol, dict):
            continue
        fields.add(symbol, "detail", detail=True)
        _symbols(fields, symbol.get("children"))


async def localize_result(translator: Any, method: str, result: Any, *, scope: str = "") -> Any:
    """Translate only the known display fields of one LSP result."""
    output = copy.deepcopy(result)
    if method == "textDocument/hover" and isinstance(output, dict):
        if isinstance(output.get(_ORIGINAL), dict) and "contents" in output[_ORIGINAL]:
            return output
        try:
            translated = await translator.translate_hover(copy.deepcopy(output), scope=scope)
        except asyncio.CancelledError:
            raise
        except Exception:
            return output
        if (isinstance(translated, dict) and "contents" in output and "contents" in translated
                and translated["contents"] != output["contents"]):
            _remember(output, "contents", output["contents"])
            output["contents"] = copy.deepcopy(translated["contents"])
            if "documentation_translation" in translated:
                if "documentation_translation" in output:
                    _remember(output, "documentation_translation", output["documentation_translation"])
                else:
                    _metadata(output)[_ADDED] = ["documentation_translation"]
                output["documentation_translation"] = copy.deepcopy(translated["documentation_translation"])
        return output

    fields = _Fields()
    if method in {"textDocument/completion", "completionItem/resolve"}:
        items = ([output] if method == "completionItem/resolve" else
                 output.get("items", []) if isinstance(output, dict) else output)
        for item in _each(items):
            fields.add(item, "documentation", contents=True)
            fields.add(item, "detail", detail=True)
    elif method == "textDocument/signatureHelp" and isinstance(output, dict):
        for signature in _each(output.get("signatures")):
            fields.add(signature, "documentation", contents=True)
            if isinstance(signature, dict):
                for parameter in _each(signature.get("parameters")):
                    fields.add(parameter, "documentation", contents=True)
    elif method in {"textDocument/codeAction", "codeAction/resolve"}:
        for action in ([output] if method.endswith("/resolve") else _each(output)):
            _action(fields, action)
    elif method in {"textDocument/codeLens", "codeLens/resolve"}:
        for lens in ([output] if method.endswith("/resolve") else _each(output)):
            if isinstance(lens, dict):
                _command(fields, lens.get("command"))
    elif method in {"textDocument/inlayHint", "inlayHint/resolve"}:
        for hint in ([output] if method.endswith("/resolve") else _each(output)):
            fields.add(hint, "tooltip", contents=True)
            if isinstance(hint, dict):
                for part in _each(hint.get("label")):
                    fields.add(part, "tooltip", contents=True)
    elif method in {"textDocument/documentLink", "documentLink/resolve"}:
        for link in ([output] if method.endswith("/resolve") else _each(output)):
            fields.add(link, "tooltip")
    elif method in {"textDocument/documentSymbol", "workspace/symbol", "textDocument/prepareCallHierarchy",
                    "textDocument/prepareTypeHierarchy", "typeHierarchy/supertypes", "typeHierarchy/subtypes"}:
        _symbols(fields, output)
    elif method == "workspaceSymbol/resolve":
        _symbols(fields, [output])
    elif method in {"callHierarchy/incomingCalls", "callHierarchy/outgoingCalls"}:
        for item in _each(output):
            if isinstance(item, dict):
                _symbols(fields, [item.get("from" if method.endswith("incomingCalls") else "to")])
    elif method == "textDocument/diagnostic" and isinstance(output, dict):
        _diagnostics(fields, output.get("items"))
        related = output.get("relatedDocuments")
        if isinstance(related, dict):
            for report in related.values():
                if isinstance(report, dict):
                    _diagnostics(fields, report.get("items"))
    elif method == "workspace/diagnostic" and isinstance(output, dict):
        for report in _each(output.get("items")):
            if isinstance(report, dict):
                _diagnostics(fields, report.get("items"))
    await fields.translate(translator, scope=scope)
    return output


async def localize_notification(translator: Any, message: Any, *, scope: str = "") -> Any:
    """Project known notification/server-request prose without touching data."""
    output = copy.deepcopy(message)
    if not isinstance(output, dict):
        return output
    method, params = output.get("method"), output.get("params")
    if not isinstance(params, dict):
        return output
    fields = _Fields()
    if method == "textDocument/publishDiagnostics":
        _diagnostics(fields, params.get("diagnostics"))
    elif method == "$/progress":
        fields.add(params.get("value"), "title")
        fields.add(params.get("value"), "message")
    elif method in {"window/showMessage", "window/logMessage", "window/showMessageRequest", "code-agent/error"}:
        fields.add(params, "message")
        if method == "window/showMessageRequest":
            for action in _each(params.get("actions")):
                fields.add(action, "title")
    elif method == "code-agent/reviewEdit":
        fields.add(params, "label")
    await fields.translate(translator, scope=scope)
    return output


async def localize_error(translator: Any, error: Any, *, scope: str = "") -> Any:
    """Localize an error object or a JSON-RPC error envelope's message only."""
    output = copy.deepcopy(error)
    if not isinstance(output, dict):
        return output
    fields = _Fields()
    fields.add(output.get("error") if isinstance(output.get("error"), dict) else output, "message")
    await fields.translate(translator, scope=scope)
    return output
