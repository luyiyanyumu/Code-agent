"""Offline checks for prose-only LSP projections and exact protocol recovery."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from code_agent.codeintel.lsp_localization import (
    localize_error, localize_notification, localize_result, restore_protocol_text,
)


META = "_code_agent_original"


class FakeTranslator:
    def __init__(self, *, failure=None, invalid=False):
        self.calls = []
        self.hover_calls = []
        self.failure = failure
        self.invalid = invalid

    async def translate_contents(self, contents, *, scope="", identifiers=(), single_words=False):
        self.calls.append((copy.deepcopy(contents), scope, set(identifiers), single_words))
        if self.failure:
            raise self.failure
        if self.invalid:
            return "not a list"
        return ["中文：" + item if isinstance(item, str)
                else {**item, "value": "中文：" + item["value"]} for item in contents]

    async def translate_hover(self, result, *, scope=""):
        self.hover_calls.append((copy.deepcopy(result), scope))
        if self.failure:
            raise self.failure
        result["contents"][1] = "返回不可修改集合。"
        result["documentation_translation"] = {"language": "zh-CN", "source": "configured-model"}
        # The projection must retain the original protocol range, even if an
        # adapter accidentally modifies unrelated fields.
        result["range"] = {"wrong": True}
        return result


def run(coro):
    return asyncio.run(coro)


def assert_roundtrip(source, localized):
    restored = restore_protocol_text(localized)
    assert restored == source
    assert restored is not localized


def test_hover_delegates_preserves_code_and_range_and_restores_translation_metadata():
    source = {"contents": [{"language": "java", "value": "Set<E> of(E e1)"}, "Returns an unmodifiable set."],
              "range": {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 4}}}
    untouched = copy.deepcopy(source)
    translator = FakeTranslator()
    output = run(localize_result(translator, "textDocument/hover", source, scope="p:main:jdtls"))
    assert output["contents"][1] == "返回不可修改集合。"
    assert output["contents"][0] == source["contents"][0]
    assert output["range"] == source["range"]
    assert output[META]["contents"] == source["contents"]
    assert translator.hover_calls[0][1] == "p:main:jdtls" and not translator.calls
    assert source == untouched
    assert_roundtrip(source, output)
    assert run(localize_result(translator, "textDocument/hover", output)) == output
    assert len(translator.hover_calls) == 1


@pytest.mark.parametrize("method,wrap", [
    ("textDocument/completion", lambda item: {"isIncomplete": True, "items": [item]}),
    ("textDocument/completion", lambda item: [item]),
    ("completionItem/resolve", lambda item: item),
])
def test_completion_batches_prose_without_changing_labels_code_or_opaque_data(method, wrap):
    item = {"label": "getValue", "labelDetails": {"detail": "(int index)", "description": "java.util.List"},
            "detail": "Returns the current value.", "documentation": {"kind": "markdown", "value": "Reads the element."},
            "insertText": "getValue(${1:index})", "filterText": "getValue", "sortText": "001",
            "textEdit": {"newText": "getValue(0)", "range": {}}, "additionalTextEdits": [{"newText": "import X;"}],
            "data": {"documentation": "opaque", META: {"message": "server-owned"}},
            "command": {"title": "Do not visit this unspecified field", "command": "java.apply", "arguments": ["raw"]}}
    source = wrap(item)
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source, scope="java"))
    translated = output if method.endswith("resolve") else output["items"][0] if isinstance(output, dict) else output[0]
    assert translated["detail"].startswith("中文：")
    assert translated["documentation"]["value"].startswith("中文：")
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 2
    assert translator.calls[0][1:] == ("java", set(), True)
    for key in ("label", "labelDetails", "insertText", "filterText", "sortText", "textEdit", "additionalTextEdits", "data", "command"):
        assert translated[key] == item[key]
    assert_roundtrip(source, output)


@pytest.mark.parametrize("detail", [
    "String", "java.util.Set<String>", "public static Set<E> of(E e1)", "value: string",
    "fn read(&self) -> Result<T>", "def read(value: str) -> str", "unsigned long long",
    "const char *", "String value", "(value: string) => number", "x = 2;", "record User(String name)",
])
def test_code_shaped_details_never_reach_translation(detail):
    translator = FakeTranslator()
    source = [{"label": "item", "detail": detail}]
    assert run(localize_result(translator, "textDocument/completion", source)) == source
    assert not translator.calls


def test_signature_labels_parameter_names_offsets_and_signatures_stay_original():
    source = {"signatures": [{"label": "write(name: str, count: int)", "documentation": "Writes the value.",
                              "parameters": [{"label": [6, 15], "documentation": {"kind": "plaintext", "value": "The target name."}},
                                             {"label": "count", "documentation": "The number of copies."}]}],
              "activeSignature": 0, "activeParameter": 1}
    translator = FakeTranslator()
    output = run(localize_result(translator, "textDocument/signatureHelp", source))
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 3
    assert output["signatures"][0]["label"] == source["signatures"][0]["label"]
    assert [p["label"] for p in output["signatures"][0]["parameters"]] == [[6, 15], "count"]
    assert_roundtrip(source, output)


def diagnostic():
    return {"message": "The method fields() from 'JsonNode' is deprecated", "severity": 2, "tags": [2],
            "code": 67108967, "source": "Java", "range": {"start": {"line": 4, "character": 9}},
            "codeDescription": {"href": "https://example.invalid/doc"}, "data": {"message": "opaque diagnostic"},
            "relatedInformation": [{"message": "Use properties() instead", "location": {"uri": "file:///a.java", "range": {}}}]}


def test_diagnostics_batch_related_prose_protect_names_and_roundtrip_for_code_actions():
    source = {"method": "textDocument/publishDiagnostics", "params": {"uri": "file:///a.java", "version": 8,
                                                                          "diagnostics": [diagnostic()]}}
    original = copy.deepcopy(source)
    translator = FakeTranslator()
    output = run(localize_notification(translator, source, scope="p:main:java"))
    actual = output["params"]["diagnostics"][0]
    assert actual["message"].startswith("中文：")
    assert actual["relatedInformation"][0]["message"].startswith("中文：")
    assert translator.calls[0][2] == {"fields", "JsonNode", "properties"}
    assert len(translator.calls) == 1
    assert source == original
    request = {"textDocument": {"uri": "file:///a.java"}, "context": {"diagnostics": [actual]}}
    assert restore_protocol_text(request)["context"]["diagnostics"] == [diagnostic()]
    assert_roundtrip(source, output)


@pytest.mark.parametrize("method", ["textDocument/codeAction", "codeAction/resolve"])
def test_actions_restore_titles_disabled_reason_diagnostics_and_command_but_not_edit_payload(method):
    action = {"title": "Replace deprecated call", "kind": "quickfix", "disabled": {"reason": "Requires a writable file"},
              "diagnostics": [diagnostic()], "data": {"title": "opaque title"},
              "command": {"title": "Apply fix", "command": "java.apply.fix", "arguments": [{"message": "opaque"}]},
              "edit": {"changes": {"file:///a.java": [{"newText": "properties()", "annotationId": "raw", "range": {}}]}}}
    source = action if method.endswith("/resolve") else [action]
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source))
    actual = output if isinstance(output, dict) else output[0]
    assert actual["title"].startswith("中文：") and actual["disabled"]["reason"].startswith("中文：")
    assert actual["command"]["title"].startswith("中文：")
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 5
    assert actual["edit"] == action["edit"] and actual["data"] == action["data"]
    assert actual["command"]["arguments"] == action["command"]["arguments"]
    assert_roundtrip(source, output)


@pytest.mark.parametrize("method", ["textDocument/codeLens", "codeLens/resolve"])
def test_lens_command_title_only(method):
    lens = {"range": {}, "command": {"title": "Three references", "command": "show.references", "arguments": ["raw"]}, "data": {"title": "raw"}}
    source = lens if method.endswith("/resolve") else [lens]
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source))
    assert translator.calls[0][0] == ["Three references"]
    assert_roundtrip(source, output)


@pytest.mark.parametrize("method", ["textDocument/inlayHint", "inlayHint/resolve"])
def test_inlay_labels_and_text_edits_remain_code_while_all_tooltips_are_batched(method):
    hint = {"position": {"line": 1, "character": 2}, "kind": 1,
            "label": [{"value": "String", "tooltip": {"kind": "markdown", "value": "The inferred type."},
                       "location": {"uri": "file:///a.java", "range": {}},
                       "command": {"title": "raw", "command": "x", "arguments": []}}],
            "tooltip": "Inferred from the initializer.", "textEdits": [{"newText": ": String"}], "data": {"tooltip": "opaque"}}
    source = hint if method.endswith("/resolve") else [hint]
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source))
    actual = output if isinstance(output, dict) else output[0]
    assert actual["label"][0]["value"] == "String"
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 2
    assert_roundtrip(source, output)


@pytest.mark.parametrize("method", ["textDocument/documentLink", "documentLink/resolve"])
def test_document_link_tooltip_preserves_uri(method):
    link = {"range": {}, "target": "https://example.invalid/Docs", "tooltip": "Open the documentation", "data": {"tooltip": "opaque"}}
    source = link if method.endswith("/resolve") else [link]
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source))
    assert translator.calls[0][0] == ["Open the documentation"]
    assert_roundtrip(source, output)


@pytest.mark.parametrize("method", ["textDocument/documentSymbol", "workspace/symbol", "textDocument/prepareCallHierarchy",
                                   "textDocument/prepareTypeHierarchy", "typeHierarchy/supertypes", "typeHierarchy/subtypes"])
def test_symbols_translate_only_clear_prose_detail_and_preserve_names_and_kinds(method):
    source = [{"name": "User", "kind": 5, "detail": "Represents an authenticated user.", "selectionRange": {},
               "children": [{"name": "read", "kind": 6, "detail": "String read(int id)"}]}]
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source))
    assert translator.calls[0][0] == ["Represents an authenticated user."]
    assert output[0]["name"] == "User" and output[0]["kind"] == 5
    assert_roundtrip(source, output)


@pytest.mark.parametrize("method", ["callHierarchy/incomingCalls", "callHierarchy/outgoingCalls"])
def test_hierarchy_only_visits_proven_symbol_detail(method):
    key = "from" if method.endswith("incomingCalls") else "to"
    source = [{key: {"name": "run", "detail": "Creates a new task.", "kind": 6, "uri": "file:///a.py"}, "fromRanges": []}]
    translator = FakeTranslator()
    output = run(localize_result(translator, method, source))
    assert translator.calls[0][0] == ["Creates a new task."]
    assert_roundtrip(source, output)


def test_pull_diagnostics_translate_reports_without_changing_report_identity():
    source = {"kind": "full", "resultId": "r1", "items": [diagnostic()],
              "relatedDocuments": {"file:///b.java": {"kind": "full", "resultId": "r2", "items": [diagnostic()]}}}
    translator = FakeTranslator()
    output = run(localize_result(translator, "textDocument/diagnostic", source))
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == 4
    assert_roundtrip(source, output)
    workspace = {"items": [{"uri": "file:///a.java", **source}]}
    output = run(localize_result(translator, "workspace/diagnostic", workspace))
    assert_roundtrip(workspace, output)


@pytest.mark.parametrize("source,texts", [
    ({"method": "$/progress", "params": {"token": "opaque", "value": {"kind": "begin", "title": "Indexing", "message": "Reading source files", "percentage": 23}}}, 2),
    ({"method": "window/logMessage", "params": {"type": 3, "message": "Starting analysis"}}, 1),
    ({"method": "window/showMessage", "params": {"type": 2, "message": "Missing dependencies"}}, 1),
    ({"method": "window/showMessageRequest", "params": {"type": 2, "message": "Update available", "actions": [{"title": "Install", "data": {"title": "raw"}}]}}, 2),
    ({"method": "code-agent/error", "params": {"message": "Server unavailable", "reconnect": True}}, 1),
    ({"method": "code-agent/reviewEdit", "params": {"label": "Apply suggested fix", "edit": {"changes": {"file:///a": [{"newText": "raw code"}]}}, "request_id": 7}}, 1),
])
def test_notification_prose_fields_keep_protocol_values(source, texts):
    translator = FakeTranslator()
    output = run(localize_notification(translator, source))
    assert len(translator.calls) == 1 and len(translator.calls[0][0]) == texts
    assert translator.calls[0][3] is True
    assert_roundtrip(source, output)


@pytest.mark.parametrize("wrap", [lambda error: error, lambda error: {"jsonrpc": "2.0", "id": 9, "error": error}])
def test_error_helper_preserves_codes_and_opaque_data_with_original_metadata(wrap):
    source = wrap({"code": -32603, "message": "The server failed", "data": {"message": "opaque"}})
    translator = FakeTranslator()
    output = run(localize_error(translator, source))
    actual = output.get("error", output)
    assert actual["message"] == "中文：The server failed"
    assert actual[META] == {"message": "The server failed"}
    assert_roundtrip(source, output)


def test_already_localized_values_do_not_overwrite_originals_or_repeat_requests():
    source = [{"title": "Replace deprecated call", "diagnostics": [diagnostic()]}]
    translator = FakeTranslator()
    first = run(localize_result(translator, "textDocument/codeAction", source))
    second = run(localize_result(translator, "textDocument/codeAction", first))
    assert second == first and len(translator.calls) == 1
    assert_roundtrip(source, second)


@pytest.mark.parametrize("method,source", [
    ("textDocument/formatting", [{"newText": "English code remains original", "range": {}}]),
    ("textDocument/rename", {"changes": {"file:///a.java": [{"newText": "RenamedSymbol"}]}}),
    ("textDocument/semanticTokens/full", {"data": [0, 0, 1, 2, 0]}),
    ("custom/unknown", {"message": "Not known to be prose", "documentation": "opaque extension"}),
    ("textDocument/completion", [{"label": "Name", "documentation": {"language": "java", "value": "SomeCode value;"}}]),
])
def test_unlisted_protocol_and_typed_code_are_not_translation_inputs(method, source):
    translator = FakeTranslator()
    assert run(localize_result(translator, method, source)) == source
    assert not translator.calls


@pytest.mark.parametrize("bad", [FakeTranslator(failure=RuntimeError("offline failure")), FakeTranslator(invalid=True)])
def test_translation_failure_preserves_complete_original_without_receipt(bad):
    source = [{"title": "Apply fix", "edit": {"changes": {}}}]
    assert run(localize_result(bad, "textDocument/codeAction", source)) == source


def test_cancellation_is_not_swallowed():
    translator = FakeTranslator(failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run(localize_error(translator, {"code": 1, "message": "Operation failed"}))


def test_oversized_lists_are_batched_instead_of_translated_once_per_item():
    source = [{"label": f"item{i}", "documentation": "Documentation " * 300} for i in range(12)]
    translator = FakeTranslator()
    output = run(localize_result(translator, "textDocument/completion", source))
    assert 1 < len(translator.calls) < len(source)
    assert all(item["documentation"].startswith("中文：") for item in output)
    assert_roundtrip(source, output)


def test_restore_metadata_cannot_inject_edits_and_does_not_mutate_source():
    source = {"title": "中文", META: {"title": "Original", "edit": {"changes": {"file:///evil": []}}},
              "data": {META: {"title": "server-owned"}}}
    snapshot = copy.deepcopy(source)
    assert restore_protocol_text(source) == {"title": "Original", "data": source["data"]}
    assert source == snapshot


def test_real_translation_adapter_batches_single_words_and_protects_diagnostic_symbol_names():
    from code_agent.codeintel.documentation_translation import DocumentationTranslator

    class Provider:
        name, model, base_url = "fixture", "fixture-model", "https://fixture.invalid"

        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools):
            assert tools == []
            texts = json.loads(messages[-1]["content"])["texts"]
            self.calls.append(texts)
            replacements = {"Rename": "重命名", "The method ": "方法 ", " from ": " 来自 ",
                            " is deprecated": " 已弃用", "Use ": "请使用 ", " instead": " 代替"}
            translated = []
            for text in texts:
                for before, after in replacements.items():
                    text = text.replace(before, after)
                translated.append(text)
            return SimpleNamespace(text=json.dumps({"translations": translated}, ensure_ascii=False), tool_calls=[])

    source = [{"title": "Rename", "diagnostics": [diagnostic()], "edit": {"changes": {}}}]
    provider = Provider()
    translator = DocumentationTranslator(lambda: provider)
    output = run(localize_result(translator, "textDocument/codeAction", source))
    assert output[0]["title"] == "重命名"
    message = output[0]["diagnostics"][0]["message"]
    assert message == "方法 fields() 来自 'JsonNode' 已弃用"
    assert output[0]["diagnostics"][0]["relatedInformation"][0]["message"] == "请使用 properties() 代替"
    assert len(provider.calls) == 1
    assert all("fields" not in text and "JsonNode" not in text and "properties" not in text for text in provider.calls[0])
    assert_roundtrip(source, output)
