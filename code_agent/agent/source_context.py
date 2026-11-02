"""Current source receipts and non-blocking read-progress hints for Harness.

This adapter never grants file access, caches tool execution, or decides when a
task ends. Only successfully dispatched native reads/edits and a deliberately
small shell-reader grammar can supply current source evidence. The ordinary
history budget remains the final authority over what fits in a model request.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_agent.agent.execution_progress import ExecutionProgress
from code_agent.agent.source_snapshot_context import SnapshotReceipt, SnapshotSource, build_source_snapshot, is_source_snapshot, required_source_chars
from code_agent.context.compact import TOOL_OMIT_MARKER, compact_messages, messages_size_chars, _complete_message_groups
from code_agent.context.safety import wrap_untrusted, _DATA_START
from code_agent.models.approval import executable_arguments
from code_agent.tools.edit_receipts import NativeEditResult
from code_agent.tools.read_receipts import NativeReadResult
from code_agent.tools.read_spec import has_read_window


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        item = path.stat()
        return (item.st_mtime_ns, item.st_size) if path.is_file() else None
    except OSError:
        return None


@dataclass
class _Receipt:
    # Logical path is retained too: changing a symlink must invalidate a view.
    versions: list[tuple[Path, Path, tuple[int, int], str]]
    text: str = ""
    supplement: str = ""
    summary: str = ""
    invalid: bool = False
    # None is a proven whole file; sets are actual delivered line numbers,
    # never the requested window or the interval enclosing disjoint matches.
    coverage: dict[Path, frozenset[int] | None] = field(default_factory=dict)


class SourceContext:
    MAX_RECEIPTS = 24
    MAX_RESULT_CHARS = 65_536

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.receipts: OrderedDict[str, _Receipt] = OrderedDict()
        self.progress = ExecutionProgress(self.workspace)
        self.seen: set[str] = set()
        self.notified: set[int] = set()
        self.snapshot_complete: set[str] = set()
        self.selected_budget = 0

    def _path(self, value: str, cwd: str = "") -> Path:
        base = self.workspace / cwd if cwd else self.workspace
        return base / value

    def begin(self, call) -> tuple[Path, Path, tuple[int, int]] | None:
        """Capture only metadata; the tool still performs all permission checks."""
        args = executable_arguments(call.arguments)
        path = None
        if call.name in {"read_file", "grep"} and isinstance(args.get("path"), str):
            path = self._path(args["path"])
        elif call.name in {"bash", "bash_persistent"}:
            reader = ExecutionProgress._shell(str(args.get("command") or ""))
            if reader and reader[1] and reader[2] is not None and (call.name != "bash_persistent" or reader[0]):
                path = self._path(reader[1], reader[0])
        if path is None:
            return None
        try:
            resolved, version = path.resolve(), _stat(path)
        except (OSError, ValueError):
            return None
        return (path, resolved, version) if version is not None else None

    def capture(self, call, result: Any, before) -> None:
        """Called before the existing runtime strips private native metadata."""
        receipt = None
        if before is not None:
            logical, resolved, version = before
            native = isinstance(result, NativeReadResult)
            shell = call.name in {"bash", "bash_persistent"} and str(result).startswith("exit_code=0\n")
            if native and result.source_stat is not None:
                version = result.source_stat
            try:
                stable = logical.resolve() == resolved and _stat(resolved) == version
            except (OSError, ValueError):
                stable = False
            if (native or shell) and stable:
                expected = next((view.expected_fingerprint for view in getattr(result, "source_views", ())
                                 if view.complete and view.expected_fingerprint), "")
                try:
                    digest = hashlib.blake2b(resolved.read_bytes(), digest_size=16).hexdigest() if version[1] <= 2_000_000 else ""
                except OSError:
                    digest = ""
                if (not expected or digest == expected) and _stat(resolved) == version:
                    receipt = _Receipt([(logical, resolved, version, digest)])
                    args = executable_arguments(call.arguments)
                    if call.name == "grep":
                        coordinates = frozenset(int(match[1]) for line in str(result).splitlines()
                                                if (match := re.match(r"^(\d+): ", line)))
                        if coordinates:
                            receipt.coverage[resolved] = coordinates
                            # Fold only duplicate source rows. Search counts,
                            # limits, clamping and exact match coordinates are
                            # distinct tool evidence even when a whole read is
                            # already visible in the same model request.
                            receipt.summary = "\n".join(line for line in str(result).splitlines()
                                if not re.match(r"^\d+: ", line) and line != "---")
                    elif call.name == "read_file":
                        views = getattr(result, "source_views", ())
                        if any(view.complete for view in views) or (not has_read_window(args)
                                and len(str(result).encode("utf-8")) == version[1]):
                            receipt.coverage[resolved] = None
                        elif has_read_window(args):
                            receipt.coverage[resolved] = frozenset(int(match[1]) for line in str(result).splitlines()
                                if (match := re.match(r"^(\d+)\t", line)))
                    elif shell:
                        reader = ExecutionProgress._shell(str(args.get("command") or ""))
                        lines = str(result).splitlines()[1:]
                        if lines and lines[0] == "[stdout]":
                            lines = lines[1:]
                        if reader and reader[3]:
                            receipt.coverage[resolved] = frozenset(int(match[1]) for line in lines
                                if (match := re.match(r"^\s*(\d+)(?:\t|: ?|\| ?)", line)))
                        elif reader and reader[2] is not None:
                            receipt.coverage[resolved] = frozenset(range(reader[2], reader[2] + len(lines)))
        if isinstance(result, NativeEditResult):
            versions, sections, coverage = [], [], {}
            for view in result.source_views:
                logical = self._path(view.path)
                try:
                    resolved, version = logical.resolve(), _stat(logical)
                    # Native edit views contain a full-file fingerprint. Check
                    # it before trusting a post-write snapshot; never infer it
                    # from a model-controlled result string or just exit code.
                    if version is None or version[1] > 2_000_000:
                        continue
                    digest = hashlib.blake2b(resolved.read_bytes(), digest_size=16).hexdigest()
                    if digest != view.expected_fingerprint or _stat(resolved) != version:
                        continue
                except (OSError, ValueError):
                    continue
                versions.append((logical, resolved, version, digest))
                lines = view.text.splitlines(keepends=True)
                coverage[resolved] = None if view.complete else frozenset(range(view.offset, view.offset + len(lines)))
                span = f"{view.offset}-{view.offset + len(lines) - 1}" if lines else "空文件"
                label = "完整当前源码" if view.complete else "当前源码片段，未显示部分仍未知"
                body = f"{view.path}；{label}；实际行号 {span}\n" + "".join(
                    f"{view.offset + index}\t{line}" for index, line in enumerate(lines))
                sections.append(body.replace("【数据块", r"\u3010数据块"))
            if versions:
                receipt = _Receipt(versions, supplement="\n" + wrap_untrusted(
                    "以下是本次实际编辑产生的源码视图，不是验证通过。\n" + "\n".join(sections), "编辑后源码"), coverage=coverage)
        if receipt is not None:
            self.receipts[call.id] = receipt
            self.receipts.move_to_end(call.id)
            while len(self.receipts) > self.MAX_RECEIPTS:
                # Repeated windows must not evict the only full view of an
                # independent file. Keep the new receipt until delivered().
                keys = list(self.receipts)
                redundant = next((key for index, key in enumerate(keys) if key != call.id and any(
                    other != key and self._covers(other, key) and (
                        not self._covers(key, other) or other_index > index)
                    for other_index, other in enumerate(keys))), None)
                self.receipts.pop(redundant if redundant is not None else keys[0])

    def delivered(self, call_id: str, result: str) -> None:
        receipt = self.receipts.get(call_id)
        if receipt is not None:
            if len(result) > self.MAX_RESULT_CHARS or result.startswith(("[tool error]", "[permission denied]", "[外部化]")):
                self.receipts.pop(call_id, None)
            else:
                receipt.text = result

    def project(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
        """Restore still-current receipts before history clipping, within budget."""
        valid = set()
        fingerprints = {}
        for call_id, receipt in self.receipts.items():
            if not receipt.text:
                continue
            for logical, resolved, version, expected in receipt.versions:
                try:
                    current = logical.resolve() == resolved and _stat(resolved) == version
                    if current and expected:
                        if resolved not in fingerprints:
                            fingerprints[resolved] = hashlib.blake2b(resolved.read_bytes(), digest_size=16).hexdigest()
                        current = fingerprints[resolved] == expected and _stat(resolved) == version
                except (OSError, ValueError):
                    current = False
                if not current:
                    receipt.invalid = True
                    break
            if not receipt.invalid:
                valid.add(call_id)
        out = []
        for message in messages:
            call_id = message.get("tool_call_id")
            receipt = self.receipts.get(call_id) if message.get("role") == "tool" else None
            if receipt is not None and receipt.text:
                content = receipt.text + receipt.supplement if call_id in valid else (
                    "[源码版本已变化] 以下是历史工具回执，不能当作当前源码；需要时重新读取对应范围。\n" + receipt.text)
                out.append({**message, "content": content})
            else:
                out.append(message)
        return out, valid

    def _covers(self, covering: str, covered: str) -> bool:
        first, second = self.receipts[covering], self.receipts[covered]
        versions = {path: (stat, digest) for _, path, stat, digest in first.versions}
        if not second.coverage or not all(versions.get(path) == (stat, digest)
                for _, path, stat, digest in second.versions):
            return False
        for path, lines in second.coverage.items():
            if path not in first.coverage:
                return False
            other = first.coverage[path]
            if other is not None and (lines is None or not lines.issubset(other)):
                return False
        return True

    def fit(self, messages: list[dict[str, Any]], max_chars: int, *, ceiling_chars: int | None = None) -> list[dict[str, Any]]:
        """Keep independent source evidence when protocol bundles crowd it out."""
        self.snapshot_complete.clear()
        messages = [message for message in messages if not is_source_snapshot(message)]
        projected, valid = self.project(messages)
        present = {m.get("tool_call_id") for m in projected if m.get("role") == "tool"}
        keys = [key for key in self.receipts if key in valid and key in present and self.receipts[key].coverage]
        records = []
        for key in keys:
            receipt = self.receipts[key]
            sources = tuple(SnapshotSource(str(path), f"{stat[0]}:{stat[1]}:{digest}", receipt.coverage[path])
                            for _, path, stat, digest in receipt.versions if path in receipt.coverage)
            if sources:
                records.append(SnapshotReceipt(key, receipt.text + receipt.supplement, sources))
        fixed = sum(messages_size_chars([m]) for m in projected if m.get("role") == "user"
                    and m.get("_context_source") != "auto" and not str(m.get("content") or "").startswith(_DATA_START))
        if ceiling_chars is not None and ceiling_chars > max_chars and records:
            slim = [{**m, "content": "[源码快照]"} if m.get("role") == "tool"
                    and m.get("tool_call_id") in keys else m for m in projected]
            recent = next((group for group in reversed(_complete_message_groups(
                [m for m in slim if m.get("role") != "system"])) if group[0].get("role") == "assistant"), [])
            needed = required_source_chars(records) + fixed + max(3000, messages_size_chars(recent))
            max_chars = max(max_chars, min(ceiling_chars, needed))
        self.selected_budget = max_chars
        fitted = self._fit_receipts(projected, max_chars)
        visible = {m.get("tool_call_id") for m in fitted if m.get("role") == "tool"
                   and m.get("tool_call_id") in valid and m.get("content") == (
                       self.receipts[m["tool_call_id"]].text + self.receipts[m["tool_call_id"]].supplement)}
        if len({path for key in keys for path in self.receipts[key].coverage}) < 2 or all(
                any(self._covers(other, key) for other in visible) for key in keys):
            return fitted
        # Snapshots are sourced data, independent of expensive historical
        # assistant/private-protocol bundles. No source is reconstructed from
        # disk here and no tool access or verification status is synthesized.
        note = "[源码快照] 本次原始回执另见独立数据快照；如标注摘录，未显示部分仍未知。"
        shortened = [{**message, "content": note} if message.get("role") == "tool"
                     and message.get("tool_call_id") in keys else message for message in projected]
        fixed = sum(messages_size_chars([m]) for m in shortened if m.get("role") == "user"
                    and m.get("_context_source") != "auto" and not str(m.get("content") or "").startswith(_DATA_START))
        groups = _complete_message_groups([m for m in shortened if m.get("role") != "system"])
        latest = next((group for group in reversed(groups) if group[0].get("role") == "assistant"), [])
        reserve = min(max_chars // 2, max(3000, messages_size_chars(latest)))
        snapshot = build_source_snapshot(records, max_chars=max_chars - fixed - reserve)
        if snapshot.message is None or len(snapshot.receipt_ids) < 2:
            return fitted
        represented = set(snapshot.receipt_ids)
        shortened = [{**message, "content": note} if message.get("role") == "tool"
                     and message.get("tool_call_id") in represented else message for message in projected]
        history = compact_messages(shortened, max_chars=max_chars - len(snapshot.message["content"]))
        latest_ids = {call.get("id") for message in latest for call in message.get("tool_calls", [])}
        retained_ids = {message.get("tool_call_id") for message in history if message.get("role") == "tool"}
        result = [*history, snapshot.message]
        if not latest_ids.issubset(retained_ids) or messages_size_chars(
                [m for m in result if m.get("role") != "system"]) > max_chars:
            return fitted
        # Raw receipts or exact same-version numbered projections are usable
        # source observations. Excerpts belong to neither set, and this never
        # grants editing permission or verification success.
        self.snapshot_complete.update(snapshot.complete_receipt_ids | snapshot.covered_receipt_ids)
        return result

    def _fit_receipts(self, messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
        """Prefer one receipt per same-version coverage, then verify it survived.

        No text is deduplicated before compaction. If a chosen covering bundle
        cannot fit, its narrower alternatives become eligible again. A folded
        duplicate always points at a complete receipt in this actual request.
        """
        projected, valid = self.project(messages)
        present = {m.get("tool_call_id") for m in projected if m.get("role") == "tool"}
        candidates = [key for key in self.receipts if key in valid and key in present]
        selected = set(candidates)
        for index, key in enumerate(candidates):
            if key not in selected:
                continue
            for other in candidates:
                if other == key or other not in selected or not self._covers(other, key):
                    continue
                # A strict superset is useful; equal views prefer the cheaper
                # representation and then the most recently executed receipt.
                if self._covers(key, other):
                    cost = lambda item: len(self.receipts[item].text) + len(self.receipts[item].supplement)
                    if cost(other) > cost(key) or cost(other) == cost(key) and candidates.index(other) < index:
                        continue
                selected.discard(key)
                break
        fitted = compact_messages(projected, max_chars=max_chars, preserve_tool_ids=selected)
        def complete(rows):
            return {m.get("tool_call_id") for m in rows if m.get("role") == "tool"
                    and m.get("tool_call_id") in valid and m.get("content") == (
                        self.receipts[m["tool_call_id"]].text + self.receipts[m["tool_call_id"]].supplement)}
        visible = complete(fitted)
        alternatives = {key for key in candidates if key not in selected
                        and not any(self._covers(other, key) for other in visible)}
        if alternatives:
            fitted = compact_messages(projected, max_chars=max_chars, preserve_tool_ids=visible | alternatives)
            visible = complete(fitted)
        # Keep the proof and its paired call/result in place; fold only a second
        # copy whose entire line coverage is represented by that visible proof.
        keep = set(visible)
        for key in reversed(candidates):
            if key not in keep:
                continue
            for other in tuple(keep):
                if other != key and self._covers(key, other):
                    keep.discard(other)
        def fold(rows):
            out, references = [], {}
            for message in rows:
                key = message.get("tool_call_id") if message.get("role") == "tool" else None
                covering = next((other for other in keep if key in valid and other != key and self._covers(other, key)), None)
                note = f"[源码重复] 同版本源码已由当前上下文的工具回执 {covering} 覆盖；重复正文已省略。" if covering else ""
                if note and self.receipts[key].summary:
                    note = self.receipts[key].summary + "\n" + note
                if note and len(note) < len(str(message.get("content") or "")):
                    out.append({**message, "content": note})
                    references[key] = covering
                else:
                    out.append(message)
            return out, references

        # One bounded refill uses the space actually released by proven
        # duplicates. Re-run from original projected history so previously
        # dropped evidence can return, then check every surviving reference.
        # If a covering bundle loses its place, keep the first valid fit; never
        # form chains of folded receipts or claim invisible source is present.
        folded, references = fold(projected)
        if references:
            refilled = compact_messages(folded, max_chars=max_chars, preserve_tool_ids=selected | keep)
            retained = complete(refilled)
            if all(references[message["tool_call_id"]] in retained for message in refilled
                   if message.get("role") == "tool" and message.get("tool_call_id") in references):
                return refilled
        return fold(fitted)[0]

    def reminder(self, messages: list[dict[str, Any]], calls: dict) -> str | None:
        """Count only new results actually visible in this model request.

        Unknown shell effects, failures and waiting do not prove progress and
        do not clear existing counters. No tool is refused and no final answer
        is forced; a new range, changed source or successful edit can proceed.
        """
        observed = False
        # Independent snapshots may outlive the original protocol bundle.
        # Only complete source data (raw or exact numbered projection) present
        # in this request counts. Excerpts cannot make a missing middle known.
        for call_id in self.snapshot_complete:
            if call_id in self.seen or call_id not in calls or call_id not in self.receipts:
                continue
            self.seen.add(call_id)
            call = calls[call_id]
            self.progress.observe(call.name, executable_arguments(call.arguments), self.receipts[call_id].text)
            observed = True
        for message in messages:
            call_id = message.get("tool_call_id")
            if message.get("role") != "tool" or call_id in self.seen or call_id not in calls:
                continue
            content = str(message.get("content") or "")
            receipt = self.receipts.get(call_id)
            if content.startswith("[源码快照]"):
                if call_id not in self.snapshot_complete:
                    continue
                content = receipt.text if receipt is not None else content
            if receipt and content.startswith(("[源码重复]", receipt.summary + "\n[源码重复]")):
                # fit() only folds a view after verifying an equivalent current
                # source receipt actually survived the model's final budget.
                content = self.receipts[call_id].text
            # A clipped/dropped receipt is not evidence that the model knows its
            # missing middle. A later fuller read must remain legitimate.
            if TOOL_OMIT_MARKER in content or content.startswith(("[源码版本已变化]", "[tool error]", "[permission denied]")):
                continue
            # A receipt clipped in an earlier request may later be restored in
            # full. Only mark evidence as observed after it is actually usable.
            self.seen.add(call_id)
            call = calls[call_id]
            self.progress.observe(call.name, executable_arguments(call.arguments), content)
            observed = True
        if not observed:
            return None
        self.progress.finish_turn()
        count = self.progress.stalled_turns
        if count == 0:
            self.notified.clear()
        if count not in {3, 5, 8, 13, 21} or count in self.notified:
            return None
        self.notified.add(count)
        return (
            f"[执行提示] 已有 {count} 轮只读操作没有增加源码或检索证据。"
            "请按用户目标，优先使用本次上下文仍保留的源码继续分析、精确编辑或相关验证；"
            "不要把已完整保留的内容再次拆成更小片段读取。"
            "若内容已被压缩、文件发生变化或确实缺少新范围，可按需重读。"
            "不确定的 Shell 和等待没有被当作进展；这不是任务完成或测试通过的证明。"
        )
