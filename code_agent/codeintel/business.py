"""One versioned business record shared by the map and editor.

Storage remains the existing feature-brief journal. This module only defines its
read/write schema; syntax descriptions and name-based guesses are not business
facts. Author comments and model text remain explicitly unreviewed candidates.
"""

from __future__ import annotations

import hashlib
import html
import json
import re

FIELDS = ("business_meaning", "detail", "inputs", "outputs", "rules", "effects", "feature_refs")
SOURCES = frozenset({"human", "comment", "model"})
STATUSES = frozenset({"current", "unreviewed", "stale", "missing"})


def version_of(row: dict) -> str:
    values = [str(row.get("content_hash") or ""), str(row.get("body_hash") or "")]
    return hashlib.sha1("\0".join(values).encode("utf-8")).hexdigest() if any(values) else ""


def is_current(item: dict, row: dict) -> bool:
    hashes = ("content_hash", "body_hash")
    return any(item.get(key) for key in hashes) and all(
        not item.get(key) or str(item[key]) == str(row.get(key) or "") for key in hashes
    )


def empty_business(row: dict, *, status: str = "missing", item: dict | None = None) -> dict:
    item = item or {}
    return {
        "business_meaning": "", "detail": "", "inputs": [], "outputs": [],
        "rules": [], "effects": [], "feature_refs": [],
        "source": source_of(item) if item else "", "evidence": [], "status": status,
        "version": version_of(row), "stored_version": version_of(item),
        "content_hash": str(row.get("content_hash") or ""),
        "body_hash": str(row.get("body_hash") or ""),
        "model": str(item.get("model") or ""), "ts": str(item.get("ts") or ""),
    }


def source_of(item: dict) -> str:
    source = str(item.get("source") or "")
    if source in SOURCES:
        return source
    return "human" if str(item.get("model") or "") == "human" else "model"


def _text(value, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是文本")
    result = value.strip()
    if len(result) > limit:
        raise ValueError(f"{label} 超过 {limit} 字")
    return result


def validated_fields(payload: dict) -> dict:
    """Bounded, intentionally plain text. Client provenance is never trusted."""
    meaning = " ".join(_text(payload.get("business_meaning", ""), 200, "功能描述").split())
    if not meaning:
        raise ValueError("功能描述不能为空")
    out = {"business_meaning": meaning, "detail": _text(payload.get("detail", ""), 6000, "详细说明")}
    for field in ("inputs", "outputs"):
        values = payload.get(field, [])
        if not isinstance(values, list) or len(values) > 80:
            raise ValueError(f"{field} 必须是最多 80 项的数组")
        out[field] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"{field} 项必须包含 name 和 meaning")
            name = _text(value.get("name", ""), 160, "名称")
            desc = _text(value.get("meaning", ""), 2000, "含义")
            if name or desc:
                out[field].append({"name": name, "meaning": desc})
    for field in ("rules", "effects", "feature_refs", "evidence"):
        values = payload.get(field, [])
        if not isinstance(values, list) or len(values) > 80:
            raise ValueError(f"{field} 必须是最多 80 项的文本数组")
        out[field] = [_text(value, 2000, field) for value in values if value != ""]
    return out


def _comment_text(doc: str) -> str:
    """Render common author markup as text without eating Java generic types."""
    literals: list[str] = []

    def literal(match: re.Match) -> str:
        literals.append(html.unescape(match.group(1).strip()))
        return f"\x00{len(literals) - 1}\x00"

    text = re.sub(r"\{@(?:code|literal)\s+([^}]*)\}", literal, doc)
    text = re.sub(r"\{@link(?:plain)?\s+([^}\s]+)(?:\s+([^}]+))?\}",
                  lambda match: match.group(2) or match.group(1), text)
    text = re.sub(r"</?(?:p|div|section|article|blockquote|h[1-6]|pre|ul|ol)(?:\s[^>]*)?\s*/?>",
                  "\n\n", text, flags=re.I)
    text = re.sub(r"<li(?:\s[^>]*)?\s*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</li\s*>|<br\s*/?\s*>", "\n", text, flags=re.I)
    # Restrict tag removal to documentation HTML: <String> in List<String> is text.
    text = re.sub(r"</?(?:a|b|strong|em|i|code|tt|span|u|s|kbd|samp|var|font|small|sup|sub|"
                  r"table|thead|tbody|tr|td|th)(?:\s[^>]*)?\s*/?>", "", text, flags=re.I)
    text = html.unescape(text)
    text = re.sub(r"!?\[([^\]\n]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"`+([^`]+)`+", r"\1", text)
    for marker in (r"\*\*", "__", "~~"):
        text = re.sub(marker + r"(\S(?:.*?\S)?)" + marker, r"\1", text)
    text = re.sub(r"(?<!\w)\*(\S(?:.*?\S)?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(\S(?:.*?\S)?)_(?!\w)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    for index, value in enumerate(literals):
        text = text.replace(f"\x00{index}\x00", value)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _comment_description(doc: str) -> tuple[str, str]:
    """Use the first sentence as the synopsis; details contain only the remainder."""
    text = _comment_text(doc)
    if not text:
        return "", ""
    first, *paragraphs = re.split(r"\n\s*\n", text, maxsplit=1)
    # Source parsers may already have flattened line breaks, so sentence punctuation
    # also separates a concise summary from the rest of the author's explanation.
    first = " ".join(first.split())
    sentence = re.search(r"[。！？!?][\"'”’）)\]]*|\.(?=\s|$)", first)
    cut = min(sentence.end() if sentence else len(first), 200)
    summary, remainder = first[:cut].strip(), first[cut:].strip()
    detail = "\n\n".join(value for value in (remainder, *paragraphs) if value).strip()
    return summary, detail[:6000]


def business_for(row: dict, item: dict | None = None) -> dict:
    """Read the latest shared record, otherwise expose author text as a candidate."""
    current = bool(item and is_current(item, row))
    if current and (source_of(item) == "human" or not str(row.get("doc") or "").strip()):
        result = empty_business(row, item=item)
        result.update({field: item.get(field, result[field]) for field in FIELDS})
        result["business_meaning"] = str(item.get("business_meaning") or item.get("text") or "")
        result["evidence"] = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        result["status"] = "current" if result["source"] == "human" else "unreviewed"
        return result
    if item and not current:
        # Do not display a previous meaning against a changed implementation.
        return empty_business(row, status="stale", item=item)
    doc = str(row.get("doc") or "").strip()
    if doc:
        result = empty_business(row)
        summary, detail = _comment_description(doc)
        extra = extra_of(row)
        parameters = extra.get("parameters") or []
        inputs = [{"name": str(value.get("name") or ""), "meaning": _comment_text(str(value.get("doc") or ""))[:2000]}
                  for value in parameters if isinstance(value, dict) and value.get("doc")][:80]
        return_doc = _comment_text(str(extra.get("return_doc") or ""))[:2000]
        result.update({"business_meaning": summary, "detail": detail,
                       "source": "comment", "status": "unreviewed",
                       "inputs": inputs, "outputs": [{"name": "return", "meaning": return_doc}] if return_doc else [],
                       "evidence": [f"{row.get('path') or ''}:{row.get('line') or 1} 作者注释"]})
        return result
    return empty_business(row)


def extra_of(row: dict) -> dict:
    value = row.get("extra") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def entity_of(row: dict) -> dict:
    extra = extra_of(row)
    selection = extra.get("selection_range") or {}
    start = selection.get("start") or {}
    return {
        **{key: row.get(key, "") for key in ("id", "kind", "name", "qname", "path", "signature", "parent")},
        "line": int(start.get("line", max(0, int(row.get("line") or 1) - 1))) + 1,
        "column": int(start.get("character", 0)) + 1,
        "selection_range": selection, "declaration_range": extra.get("declaration_range") or {},
        "version": version_of(row),
    }


def declaration_at(rows: list[dict], line: int, column: int, name: str = "") -> dict | None:
    """Only the exact declaration name range is binding evidence, never its body."""
    point = (line - 1, column - 1)
    candidates = []
    for row in rows:
        selection = extra_of(row).get("selection_range") or {}
        start, end = selection.get("start"), selection.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        a, b = (start.get("line", -1), start.get("character", -1)), (end.get("line", -1), end.get("character", -1))
        if a <= point < b and (not name or str(row.get("name") or "") == name):
            candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None
