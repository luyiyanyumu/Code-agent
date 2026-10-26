"""Process-local file responsibility, independent of conversations and project IDs.

Scopes are stored per activation by ProjectTeam. This table only describes
currently executing stages and individual Lead tool calls; it is not an OS
sandbox or a replacement for the registry's short path mutexes.
"""

from __future__ import annotations

import ntpath
import os
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    path: str
    directory: bool = False


def actual_target(workspace: Path, raw: str, *, directory: bool = False) -> Target:
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return Target(os.path.normcase(str(path.resolve())), directory)


def overlaps(left: Target, right: Target) -> bool:
    if left.path == right.path:
        return True
    return ((left.directory and right.path.startswith(left.path.rstrip(os.sep) + os.sep))
            or (right.directory and left.path.startswith(right.path.rstrip(os.sep) + os.sep)))


def contains(scope: Target, target: Target) -> bool:
    return ((scope.path == target.path and (scope.directory or not target.directory))
            or (scope.directory and target.path.startswith(scope.path.rstrip(os.sep) + os.sep)))


def normalize_paths(workspace: Path, values: list[str], *, infer_directories: bool = True) -> list[str]:
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("write_paths 必须是最多 64 项的路径数组；空数组表示只读")
    root = workspace.resolve()
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 1024:
            raise ValueError("write_paths 每项必须是 1 到 1024 字符的相对路径")
        text = raw.strip().replace("\\", "/")
        if ntpath.splitdrive(text)[0] or text.startswith("/") or any(c in text for c in "*?[]:\x00"):
            raise ValueError("write_paths 只接受工作区相对文件或目录，不接受绝对路径及通配符")
        parts = text.split("/")
        if ".." in parts:
            raise ValueError("write_paths 不能包含上级目录 ..")
        clean = "/".join(part for part in parts if part not in ("", ".")) or "."
        resolved = (root / clean).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("write_paths 不能通过符号链接越出工作区")
        directory = clean == "." or text.endswith("/") or (infer_directories and resolved.is_dir())
        clean = clean + "/" if directory and clean != "." else clean
        if clean not in result:
            result.append(clean)
    return result


def scope_targets(workspace: Path, paths: list[str]) -> tuple[Target, ...]:
    # Re-resolve for every admission/write. A symlink changed since delegation
    # must never silently expand the stored relative authority.
    checked = normalize_paths(workspace, paths, infer_directories=False)
    return tuple(actual_target(workspace, path, directory=path == "." or path.endswith("/")) for path in checked)


@dataclass(frozen=True)
class Claim:
    session_id: str
    activation_id: int
    targets: tuple[Target, ...]
    title: str = ""


class FileOwnership:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._claims: dict[object, Claim] = {}

    def blockers(self, session_id: str, targets: tuple[Target, ...]) -> list[Claim]:
        with self._lock:
            return [claim for claim in self._claims.values() if claim.session_id != session_id
                    and any(overlaps(a, b) for a in targets for b in claim.targets)]

    def claim(self, key: object, claim: Claim) -> bool:
        with self._lock:
            if self.blockers(claim.session_id, claim.targets):
                return False
            self._claims[key] = claim
            return True

    def release(self, key: object) -> None:
        with self._lock:
            self._claims.pop(key, None)

    def release_session(self, session_id: str) -> None:
        with self._lock:
            for key, claim in list(self._claims.items()):
                if claim.session_id == session_id:
                    self._claims.pop(key, None)

    def held(self, key: object) -> bool:
        with self._lock:
            return key in self._claims and bool(self._claims[key].targets)

    def held_by(self, session_id: str, activation_id: int) -> bool:
        with self._lock:
            return any(claim.session_id == session_id and claim.activation_id == activation_id and claim.targets
                       for claim in self._claims.values())

    def covers(self, key: object, targets: tuple[Target, ...]) -> bool:
        with self._lock:
            claim = self._claims.get(key)
            return claim is not None and all(any(contains(scope, target) for scope in claim.targets) for target in targets)
