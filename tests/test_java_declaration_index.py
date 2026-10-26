"""Java identity must survive line shifts and must not join unrelated names."""
from __future__ import annotations

import json

from code_agent.codeintel.parsers import is_strategy_current, parse_file
from code_agent.codeintel.store import SymbolIndex


JAVA = '''package example.orders;
import java.util.List;
import static example.Flags.ENABLED;
import static example.Flags.*;
import example.other.*;

/** Order operations. */
public class Orders {
    /** Maximum number of results. */
    private int limit = 20, retries = 2;

    /** Load an order.
     * @param id Order identifier.
     * @return Loaded order title.
     */
    public String load(final Long id) {
        String idText = id.toString();
        return idText;
    }

    public String load(String id) { return id; }
    public Orders(int limit) { this.limit = limit; }
    public <T extends Number> List<T> batch(@Marker(values={"a", "b"}) T... values) {
        return List.of(values);
    }

    interface Reader { String read(Long id); }
    enum State { /** Completed. */ DONE, READY(1) { void hidden() {} };
        private int code;
        State(int code) { this.code = code; }
    }
    /** Single order.
     * @param title Display title.
     * @param amount Amount in cents.
     */
    record Item(String title, long amount) {
        Item { if (amount < 0) throw new IllegalArgumentException(); }
    }
    @interface Marker { String[] values() default {}; }
}
'''


def parsed(text=JAVA):
    result = parse_file("src/example/orders/Orders.java", "java", text)
    assert result["error"] == ""
    assert result["strategy"] == "java-lexical-v1"
    return result


def by_name(text=JAVA):
    return {s["qname"]: s for s in parsed(text)["symbols"]}


def write_index(index, text):
    return index.apply_parse(rel="src/example/orders/Orders.java", lang="java", loc=len(text.splitlines()), size=len(text), mtime=1, sha1="fixture", parsed=parsed(text), edges=[], source=text)


def test_owner_and_complete_overload_signature_never_collide():
    rows = by_name()
    long_method = rows["example.orders.Orders.load(Long)"]
    string_method = rows["example.orders.Orders.load(String)"]
    assert long_method["id"] != string_method["id"]
    assert long_method["parent"] == rows["example.orders.Orders"]["id"]
    assert rows["example.orders.Orders.Reader.read(Long)"]["parent"] == rows["example.orders.Orders.Reader"]["id"]
    assert long_method["params"] == ["id"]
    assert long_method["returns"] == "String"
    assert rows["example.orders.Orders.batch(T[])"]["extra"]["parameters"][0]["type"] == "T..."
    assert all(s["confidence"] == "medium" and s["extra"]["lexical_identity"] for s in rows.values())


def test_symbol_and_doc_source_attachment():
    rows = by_name()
    assert rows["example.orders.Orders"]["doc"] == "Order operations."
    assert rows["example.orders.Orders.limit"]["doc"] == "Maximum number of results."
    method = rows["example.orders.Orders.load(Long)"]
    assert method["doc"] == "Load an order."
    assert method["extra"]["return_doc"] == "Loaded order title."
    parameter = rows["example.orders.Orders.load(Long)@param:0:id"]
    assert parameter["doc"] == "Order identifier."
    assert parameter["parent"] == method["id"]
    assert rows["example.orders.Orders.load(String)@param:0:id"]["doc"] == ""
    assert rows["example.orders.Orders.Item.title"]["doc"] == "Display title."
    assert rows["example.orders.Orders.Item.amount"]["doc"] == "Amount in cents."


def test_nested_records_enums_annotation_and_constructor_members():
    rows = by_name()
    assert rows["example.orders.Orders.Reader"]["kind"] == "interface"
    assert rows["example.orders.Orders.State"]["kind"] == "enum"
    assert rows["example.orders.Orders.State.DONE"]["kind"] == "enum_member"
    assert rows["example.orders.Orders.State.DONE"]["doc"] == "Completed."
    assert rows["example.orders.Orders.Item"]["kind"] == "record"
    assert rows["example.orders.Orders.Item.title"]["kind"] == "record_component"
    assert rows["example.orders.Orders.Item.Item(String,long)"]["kind"] == "constructor"
    assert rows["example.orders.Orders.Marker"]["kind"] == "annotation"
    assert rows["example.orders.Orders.Marker.values()"]["returns"] == "String[]"
    assert not any(s["name"] in {"hidden", "idText", "of", "toString"} for s in rows.values())


def test_stable_id_and_hash_after_leading_line_shift():
    before, after = by_name(), by_name("\n\n// Leading note.\n" + JAVA)
    assert before.keys() == after.keys()
    for name, row in before.items():
        shifted = after[name]
        assert shifted["id"] == row["id"]
        assert shifted["content_hash"] == row["content_hash"]
        assert shifted["body_hash"] == row["body_hash"]
        assert shifted["line"] == row["line"] + 3
        assert shifted["extra"]["selection_range"]["start"]["line"] == row["extra"]["selection_range"]["start"]["line"] + 3


def test_real_end_scope_and_body_hash_including_parameter_context():
    before = by_name()
    after = by_name(JAVA.replace("return idText;", 'return "changed";'))
    method = "example.orders.Orders.load(Long)"
    assert before[method]["end_line"] > before[method]["line"]
    assert before[method]["content_hash"] == after[method]["content_hash"]
    assert before[method]["body_hash"] != after[method]["body_hash"]
    assert before[method + "@param:0:id"]["body_hash"] != after[method + "@param:0:id"]["body_hash"]
    assert before["example.orders.Orders.load(String)"]["body_hash"] == after["example.orders.Orders.load(String)"]["body_hash"]


def test_same_line_declarations_hash_only_the_own_source_span():
    before = by_name("class X { int a() { return 1; } int b() { return 2; } }")
    after = by_name("class X { int a() { return 9; } int b() { return 2; } }")
    assert before["X.a()"]["body_hash"] != after["X.a()"]["body_hash"]
    assert before["X.b()"]["body_hash"] == after["X.b()"]["body_hash"]


def test_exact_lsp_utf16_selection_and_crlf():
    text = 'class Demo { String marker="😀"; int 金额; void save(long 编号) {} }\r\n'
    rows = by_name(text)
    lines = text.splitlines()
    for name in ("Demo.金额", "Demo.save(long)", "Demo.save(long)@param:0:编号"):
        row = rows[name]
        span = row["extra"]["selection_range"]
        assert span["start"]["line"] == span["end"]["line"] == 0
        encoded = lines[0].encode("utf-16-le")
        a, b = span["start"]["character"] * 2, span["end"]["character"] * 2
        assert encoded[a:b].decode("utf-16-le") == row["name"]
        assert span["start"]["character"] > lines[0].find(row["name"])


def test_comments_strings_text_blocks_initializers_and_local_classes_not_indexed():
    text = '''class Demo {
      /* class Ghost { void fake(){} } */
      String literal = "class Ghost { void fake(){} }";
      String block = """
           class Ghost { void fake(){} }
           """;
      Runnable callback = new Runnable() { public void run() {} };
      static { System.out.println("static"); }
      { System.out.println("instance"); }
      int[] codes = {1, 2};
      java.util.function.Supplier<Integer> value = () -> { return 1; };
      void real() { class Local { void hidden(){} } }
    }'''
    rows = by_name(text)
    assert {s["name"] for s in rows.values()} == {"Demo", "literal", "block", "callback", "codes", "value", "real"}


def test_array_declarators_and_normalized_generics():
    rows = by_name("class X { List<String> first[], second; int[] a,b[]; void apply(java.util.Map<String, List<? extends Number>> data, int nums[], String... rest) {} }")
    assert rows["X.first"]["returns"] == "List<String>[]"
    assert rows["X.second"]["returns"] == "List<String>"
    assert rows["X.a"]["returns"] == "int[]"
    assert rows["X.b"]["returns"] == "int[][]"
    assert "X.apply(java.util.Map<String,List<? extends Number>>,int[],String[])" in rows


def test_explicit_and_static_import_identity_retained():
    imports = parsed()["imports"]
    assert [i["module"] for i in imports] == ["java.util.List", "example.Flags", "example.Flags", "example.other.*"]
    assert imports[1]["target"] == "example.Flags.ENABLED"
    assert imports[1]["names"] == ["ENABLED"]
    assert imports[1]["static"] is True
    assert imports[2]["target"] == "example.Flags.*"


def test_duplicate_declarations_and_children_are_omitted():
    rows = by_name("class X { void load(String id){} void load(String id){} int valid; }")
    assert set(rows) == {"X", "X.valid"}
    rows = by_name("class X {} class X { void hidden() {} }")
    assert rows == {}


def test_unbalanced_file_has_no_unreliable_symbols():
    result = parse_file("Broken.java", "java", "class Broken { void run() {")
    assert result["symbols"] == []
    assert "unbalanced" in result["error"]
    assert result["confidence"] == "low"


def test_modern_modifiers_annotations_and_default_attribute_values():
    rows = by_name('''sealed class Root permits Child {
      @Deprecated public String get(String value) @Marker [] { return new String[]{value}; }
    }
    non-sealed class Child extends Root {}
    @interface Marker { String label() default "note"; int[] counts() default {1,2}; }
    ''')
    assert rows["Child"]["kind"] == "class"
    assert rows["Child"]["bases"] == ["Root"]
    assert rows["Root.get(String)"]["returns"] == "String[]"
    assert rows["Marker.label()"]["returns"] == "String"
    assert rows["Marker.counts()"]["returns"] == "int[]"


def test_java_body_change_emits_updates_for_callable_and_parameter(tmp_path):
    index = SymbolIndex(tmp_path / "index.db")
    try:
        write_index(index, JAVA)
        before = index.version()
        changes = write_index(index, JAVA.replace("return idText;", 'return "changed";'))
        updated = {c["node_id"] for c in changes if c["kind"] == "sym_updated"}
        prefix = "sym:src/example/orders/Orders.java#example.orders.Orders.load(Long)"
        assert prefix in updated
        assert prefix + "@param:0:id" in updated
        assert not any("load(String)" in sid for sid in updated)
        assert index.version() == before + 1
    finally:
        index.close()


def test_other_javadoc_rules_also_invalidate_parameter_owner_context():
    text = "class X { /** Save.\n * @param id Order id.\n * @throws Error When closed.\n */ void save(Long id) {} }"
    before = by_name(text)
    after = by_name(text.replace("When closed.", "When deleted."))
    for name in ("X.save(Long)", "X.save(Long)@param:0:id"):
        assert before[name]["body_hash"] != after[name]["body_hash"]


def test_import_context_change_invalidates_unresolved_lexical_type_meaning():
    text = "import first.Order; class X { void save(Order order) {} }"
    before = by_name(text)
    after = by_name(text.replace("first.Order", "second.Order"))
    for name in ("X.save(Order)", "X.save(Order)@param:0:order"):
        assert before[name]["id"] == after[name]["id"]
        assert before[name]["content_hash"] != after[name]["content_hash"]


def test_store_persists_exact_extra_and_matches_live_hashes(tmp_path):
    index = SymbolIndex(tmp_path / "index.db")
    try:
        write_index(index, JAVA)
        for symbol in parsed()["symbols"]:
            row = index.symbol(symbol["id"])
            assert json.loads(row["extra"]) == symbol["extra"]
            assert row["body_hash"] == symbol["body_hash"]
            assert row["content_hash"] == symbol["content_hash"]
        before = index.symbols_for("src/example/orders/Orders.java")
        assert write_index(index, "\n\n" + JAVA) == []
        after = index.symbols_for("src/example/orders/Orders.java")
        assert [r["id"] for r in before] == [r["id"] for r in after]
        assert all(b["line"] + 2 == a["line"] for b, a in zip(before, after))
    finally:
        index.close()


def test_strategy_upgrade_requires_reparse_only_for_changed_parser():
    assert not is_strategy_current("java", "generic")
    assert is_strategy_current("java", "java-lexical-v1")
    assert is_strategy_current("python", "ast")
    assert is_strategy_current("json", "")
