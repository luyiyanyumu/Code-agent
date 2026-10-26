"""Author explanations stay available beyond a brief map headline."""

from __future__ import annotations

import pytest

from code_agent.codeintel import purpose
from code_agent.codeintel.parsers import generic_parser


class JavaIndex:
    def __init__(self, source: str):
        self.source = source
        self.path = "src/AgentService.java"
        self.rows = [dict(row, path=self.path) for row in generic_parser.parse(self.path, source)["symbols"]]

    def file_row(self, rel):
        return {"path": self.path, "lang": "java", "loc": len(self.source.splitlines())} if rel == self.path else None

    def symbols_for(self, rel):
        return self.rows if rel == self.path else []

    def edges(self, **kwargs):
        return []

    def symbol(self, sid):
        return next((row for row in self.rows if row["id"] == sid), None)


def describe(source: str):
    index = JavaIndex(source)
    return purpose.describe_file(index, read_text=lambda rel: source, rel=index.path)


def test_java_javadoc_after_many_imports_preserves_full_author_explanation():
    prefix = "package demo;\n\n" + "\n".join(f"import demo.Type{i};" for i in range(70)) + "\n\n"
    source = prefix + """/**
 * 智能体编排服务。
 * <ul>
 * <li>polish：Markdown 语言润色，无工具。</li>
 * <li>chat：工具对话循环，允许读取及新增/更新，不提供删除。</li>
 * </ul>
 */
@Service
@RequiredArgsConstructor
public class AgentService {
    /** 单次调用最多允许的轮数。 */
    private static final int MAX_ROUNDS = 8;
}
"""
    out = describe(source)
    assert out["headline"].startswith("智能体编排服务。")
    doc = out["documentation"]
    assert doc["source"] == "类型 Javadoc"
    assert doc["line"] == len(prefix.splitlines()) + 1
    assert "- polish：Markdown 语言润色，无工具。" in doc["text"]
    assert "- chat：工具对话循环，允许读取及新增/更新，不提供删除。" in doc["text"]
    assert "<li>" not in doc["text"]
    assert "最多允许的轮数" not in doc["text"]
    assert any("启发式" in note for note in out["notes"])


@pytest.mark.parametrize("annotation", [
    '@Service',
    '@demo.Service(value = "text (with parentheses)")',
    '@Options(\n  value = @Nested(name = "x"),\n  groups = {"a", "b"}\n)',
])
def test_java_declaration_annotations_keep_javadoc_associated(annotation):
    source = f"package demo;\n/** 执行 {{@code foo()}}，参见 {{@link demo.Tool 工具}}。 */\n{annotation}\npublic class AgentService {{}}\n"
    out = describe(source)
    assert out["documentation"]["text"] == "执行 foo()，参见 工具。"
    assert out["documentation"]["line"] == 2


def test_java_does_not_present_a_member_or_later_helper_as_file_purpose():
    source = """package demo;
public class AgentService {
    /** 这是内部字段的说明。 */
    private int count;
}
/** 这是辅助类型的说明。 */
class Helper {}
"""
    out = describe(source)
    assert out["headline"].startswith("java 文件：")
    assert "documentation" not in out


def test_javadoc_is_not_attached_across_unrelated_code():
    source = """/** 许可证说明。 */
package demo;
import demo.Other;
public class AgentService {}
"""
    doc, line = purpose._java_declaration_doc(source, 4)
    assert (doc, line) == ("", 0)


def test_java_symbol_also_leads_with_author_explanation_instead_of_signature():
    source = "package demo;\n/** 智能体编排服务。\n * chat：调用工具。\n */\n@Service\npublic class AgentService {}\n"
    index = JavaIndex(source)
    out = purpose.describe(index, read_text=lambda rel: source, symbol_id=index.rows[0]["id"])
    assert "智能体编排服务。" in out["headline"]
    assert "public class" not in out["headline"]
    assert "chat：调用工具。" in out["documentation"]["text"]
    assert next(f for f in out["facts"] if f["label"] == "签名")["text"] == "public class AgentService {}"


def test_java_documentation_locates_actual_declaration_when_index_lines_are_old():
    old_source = "package demo;\n/** 智能体编排服务。 */\n@Service\npublic class AgentService {}\n"
    index = JavaIndex(old_source)
    current_source = old_source.replace("\n", "\n\n")
    out = purpose.describe_file(index, read_text=lambda rel: current_source, rel=index.path)
    assert out["documentation"]["line"] == 3
    assert out["headline"].startswith("智能体编排服务。")
    symbol = purpose.describe(index, read_text=lambda rel: current_source, symbol_id=index.rows[0]["id"])
    assert symbol["documentation"]["line"] == 3


def test_java_type_text_inside_comments_or_strings_does_not_steal_documentation():
    source = """package demo;
// public class AgentService {}
/** 智能体编排服务。 */
@Service(value = "public class AgentService {}")
public class AgentService {}
"""
    out = describe(source)
    assert out["documentation"]["line"] == 3


def test_javascript_inner_comment_is_not_a_file_header():
    assert purpose._leading_comment("export function work() {\n// Skip failed jobs.\n}\n") == ""
    assert purpose._leading_comment("<script setup>\n// Workspace tools.\nconst ready = true\n</script>") == "Workspace tools."
