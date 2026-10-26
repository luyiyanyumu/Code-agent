"""记忆检索（s12 简化版）：关键词重叠打分，BM25 思路、零依赖。

为什么不直接全量注入：记忆无脑塞 prompt 会挤爆上下文窗口且引入噪音——
先按当前 query 检索 top-K，再给预算内的条目。接口签名固定，
后续升级向量检索只换实现、不动调用方。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    """混合分词：ASCII 取单词；中文取相邻二元组（bigram）。

    中文无空格分词，整句当一个 token 会让重叠打分完全失效；
    bigram 是零依赖方案里召回/精度平衡最好的。
    """
    tokens: list[str] = []
    # Keep full identifiers as well as their word components: both
    # FileStorageService and file_storage_service should match "file storage".
    expanded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", expanded)
    for run in _TOKEN_RE.findall(text.lower()):
        if "\u4e00" <= run[0] <= "\u9fff":
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            tokens.append(run)
    for run in _TOKEN_RE.findall(expanded.lower()):
        if run and not ("\u4e00" <= run[0] <= "\u9fff"):
            for part in run.split("_"):
                if part and part not in tokens:
                    tokens.append(part)
    return tokens


@dataclass
class MemoryEntry:
    scope: str      # "workspace" | "user"
    text: str
    score: float = 0.0
    id: str = ""
    label: str = ""
    category: str = "general"
    source: str = ""
    pinned: bool = False
    enabled: bool = True
    match: str = "keyword"


def rank_positions(entries: list[tuple[str, str]], query: str) -> list[tuple[int, float]]:
    """关键词路的**完整名次**：返回所有有分（>0）条目的**下标**与分数，按分降序。

    为什么返回下标而不是 scope：记忆的 scope 只有 `user`/`workspace` 两种，
    **同一个 scope 会有多条**——按 scope 回查会把"第二条的分数"配到"第一条的文本"上
    （真机踩到：`retrieve` 拿错文本，排序结果看起来才对）。下标没有这个歧义。
    """
    if not entries or not query.strip():
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    doc_tokens = [_tokenize(text) for _, text in entries]
    singles = {token for token in q_tokens if len(token) == 1 and "\u4e00" <= token <= "\u9fff"}
    if singles:
        for index, (_scope, text) in enumerate(entries):
            doc_tokens[index].extend(token for token in singles if token in text and token not in doc_tokens[index])
    n_docs = len(entries)
    df: Counter = Counter()
    q_set = set(q_tokens)
    for toks in doc_tokens:
        for t in set(toks):
            if t in q_set:
                df[t] += 1

    scored: list[tuple[int, float]] = []
    for index, toks in enumerate(doc_tokens):
        if not toks:
            continue
        tf = Counter(toks)
        score = 0.0
        for t in q_set:
            if df[t] == 0:
                continue
            idf = math.log(1 + n_docs / df[t])
            score += idf * tf[t] / (len(toks) ** 0.5)
        if score > 0:
            scored.append((index, round(score, 4)))
    # 只按分数排（稳定排序）：同分时保持调用方给的顺序，结果可复现
    scored.sort(key=lambda item: -item[1])
    return scored


def rank(entries: list[tuple[str, str]], query: str) -> list[tuple[str, float]]:
    """(引用, 分数) 形式的名次，给"引用唯一"的调用方（如知识库的 `human:doc#3`）。

    融合排序（retrieval.hybrid）需要"名次"而不是"top-K"：只拿前 K 条会把另一路的
    第一名挤掉，融合就失真了。记忆那种"引用不唯一"的场景请用 `rank_positions`。
    """
    return [(str(entries[i][0]), score) for i, score in rank_positions(entries, query)]


def retrieve(
    entries: list, query: str, top_k: int = 5, max_chars: int = 4000,
) -> list[MemoryEntry]:
    """entries: [(scope, text)]，返回按相关度排序的 top-K。

    打分：query 与条目的 token 重叠度（IDF 加权 + 长度归一）。
    无关键词命中时返回空列表——宁缺毋滥，不往上下文塞无关记忆。
    """
    if int(top_k) <= 0 or int(max_chars) <= 0:
        return []
    priority = {"session": 3, "project": 2, "workspace": 2, "user": 1}
    unique: dict[str, MemoryEntry] = {}
    for item in entries:
        if isinstance(item, MemoryEntry):
            candidate = MemoryEntry(**vars(item))
        elif isinstance(item, dict):
            candidate = MemoryEntry(scope=str(item.get("scope") or "workspace"),
                text=str(item.get("content") or item.get("text") or ""),
                id=str(item.get("id") or ""), label=str(item.get("label") or ""),
                category=str(item.get("category") or "general"), source=str(item.get("source") or ""),
                pinned=item.get("pinned") is True, enabled=item.get("enabled", True) is True)
        else:
            scope, text = item
            candidate = MemoryEntry(scope=str(scope), text=str(text))
        if not candidate.enabled or not candidate.text.strip():
            continue
        key = " ".join(unicodedata.normalize("NFC", candidate.text).split())
        previous = unique.get(key)
        if previous is None:
            unique[key] = candidate
        elif priority.get(candidate.scope, 0) > priority.get(previous.scope, 0):
            candidate.pinned = candidate.pinned or previous.pinned
            unique[key] = candidate
        elif candidate.pinned:
            previous.pinned = True
    candidates = list(unique.values())
    scores = dict(rank_positions([(entry.scope, entry.text) for entry in candidates], query))
    ordered = [index for index, entry in enumerate(candidates) if entry.pinned or index in scores]
    ordered.sort(key=lambda index: (not candidates[index].pinned, -scores.get(index, 0),
                                    -priority.get(candidates[index].scope, 0), index))
    selected: list[MemoryEntry] = []
    remaining = int(max_chars)
    for index in ordered[:int(top_k)]:
        if remaining <= 0:
            break
        candidate = candidates[index]
        candidate.score = scores.get(index, 0)
        candidate.match = "pinned" if candidate.pinned else "keyword"
        content = candidate.text
        if len(content) > remaining:
            marker = "…[记忆摘录省略]"
            content = content[:max(0, remaining - len(marker))] + marker[:remaining]
        candidate.text = content
        remaining -= len(content)
        selected.append(candidate)
    return selected
