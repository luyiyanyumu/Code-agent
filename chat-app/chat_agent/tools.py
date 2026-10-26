"""Small, session-scoped tools for the standalone conversation service."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any


class ToolError(ValueError):
    """A safe error that can be shown to the model and the user."""


def safe_root(config: Any, session_id: str, area: str) -> Path:
    """Reject symlink/junction redirection, including at the session directory itself."""
    home = Path(config.home).resolve()
    try:
        root = Path(config.session_dir(session_id) if area == "uploads" else config.artifact_dir(session_id))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not root.is_absolute() or not root.is_relative_to(home):
        raise ToolError("The conversation directory is outside this service's data directory.")
    current = home
    for part in root.relative_to(home).parts:
        current = current / part
        if current.is_symlink():
            raise ToolError("Linked conversation directories are not allowed.")
        try:
            attrs = getattr(current.lstat(), "st_file_attributes", 0)
            if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ToolError("Linked conversation directories are not allowed.")
        except FileNotFoundError:
            pass
    resolved = root.resolve()
    if not resolved.is_relative_to(home):
        raise ToolError("The conversation directory is outside this service's data directory.")
    return resolved


def scoped_path(root: Path, relative: str) -> Path:
    """Resolve a portable relative path while rejecting Windows and POSIX escapes."""
    if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
        raise ToolError("A non-empty relative filename is required.")
    if "\\" in relative or ":" in relative:
        raise ToolError("Absolute paths and Windows path separators are not allowed.")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise ToolError("The file must stay inside this session's directory.")
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ToolError("The file must stay inside this session's directory.")
    return path


def upload_path(config: Any, session_id: str, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.startswith("uploads/"):
        raise ToolError("Only files uploaded to this session can be read.")
    session = safe_root(config, session_id, "uploads")
    path = scoped_path(session, relative)
    uploads_raw = session / "uploads"
    if uploads_raw.is_symlink() or getattr(uploads_raw, "is_junction", lambda: False)():
        raise ToolError("Linked upload directories are not allowed.")
    uploads = uploads_raw.resolve()
    if not path.is_relative_to(uploads) or path == uploads:
        raise ToolError("Only files uploaded to this session can be read.")
    # A linked upload directory must not redirect reads outside the session.
    if not uploads.is_relative_to(session):
        raise ToolError("The uploads directory points outside this session.")
    return path


TOOL_SCHEMAS = [
    {"name": "list_files", "description": "List files uploaded to this conversation; no access to local project files.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "read_file", "description": "Read a UTF-8 text file uploaded to this conversation. Use a path returned by list_files.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}},
    {"name": "deliver_file", "description": "Create a downloadable UTF-8 text artifact in this conversation. Requires approval in read-only mode.",
     "parameters": {"type": "object", "properties": {"filename": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["filename", "content"], "additionalProperties": False}},
    {"name": "todo_write", "description": "Replace this turn's task checklist. At most one item may be in progress.",
     "parameters": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {
         "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
         "required": ["content", "status"], "additionalProperties": False}}}, "required": ["todos"], "additionalProperties": False}},
    {"name": "ask_user_question", "description": "Ask one to three questions and wait for the user's answers. Provide stable question IDs.",
     "parameters": {"type": "object", "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 3,
         "items": {"type": "object", "properties": {"id": {"type": "string"}, "header": {"type": "string"},
             "question": {"type": "string"}, "multi_select": {"type": "boolean"}, "options": {"type": "array", "items": {
                 "type": "object", "properties": {"label": {"type": "string"}, "description": {"type": "string"}}, "required": ["label"]}}},
             "required": ["id", "question"]}}}, "required": ["questions"], "additionalProperties": False}},
]


def tool_schemas() -> list[dict[str, Any]]:
    """Return independent copies for API catalog and provider adapters."""
    return json.loads(json.dumps(TOOL_SCHEMAS))


def validate_questions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise ToolError("Ask one to three questions.")
    questions: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ToolError("Each question must be an object.")
        qid = str(item.get("id", "")).strip()
        question = str(item.get("question", "")).strip()
        if not qid or len(qid) > 100 or qid in ids or not question or len(question) > 2000:
            raise ToolError("Questions need unique IDs and a non-empty question, within length limits.")
        options = item.get("options", [])
        if not isinstance(options, list) or len(options) > 8:
            raise ToolError("Provide at most eight options per question.")
        normalized = []
        for option in options:
            if isinstance(option, str):
                option = {"label": option}
            if not isinstance(option, dict) or not str(option.get("label", "")).strip():
                raise ToolError("Each option needs a label.")
            label = str(option["label"]).strip()
            description = str(option.get("description", ""))
            if len(label) > 200 or len(description) > 1000 or label in {o["label"] for o in normalized}:
                raise ToolError("Option labels must be unique and within length limits.")
            normalized.append({"label": label, "description": description})
        ids.add(qid)
        questions.append({"id": qid, "header": str(item.get("header", ""))[:100], "question": question,
                          "multi_select": item.get("multi_select") is True, "options": normalized})
    return questions


class ToolExecutor:
    """Filesystem tools cannot reach either the coding service or arbitrary local files."""

    def __init__(self, config: Any):
        self.config = config

    def execute(self, session_id: str, name: str, arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict):
            raise ToolError("Tool arguments must be an object.")
        if name == "list_files":
            root = safe_root(self.config, session_id, "uploads") / "uploads"
            if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
                raise ToolError("Linked upload directories are not allowed.")
            if not root.resolve().is_relative_to(safe_root(self.config, session_id, "uploads")):
                raise ToolError("The uploads directory is outside this conversation.")
            if not root.exists():
                return "[]"
            files = []
            for path in sorted(root.iterdir()):
                relative = "uploads/" + path.name
                try:
                    resolved = upload_path(self.config, session_id, relative)
                    if resolved.is_file():
                        files.append({"path": relative, "size": resolved.stat().st_size})
                except (OSError, ToolError):
                    continue
                if len(files) >= 200:
                    break
            return json.dumps(files, ensure_ascii=False)
        if name == "read_file":
            path = upload_path(self.config, session_id, arguments.get("path", ""))
            if not path.is_file():
                raise ToolError("Uploaded file was not found.")
            with path.open("rb") as file:
                data = file.read(256_001)
            if len(data) > 256_000:
                raise ToolError("Text files over 256 KB must be split into smaller uploads.")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ToolError("This file is not UTF-8 text; binary document parsing is not available.") from exc
            if "\x00" in text:
                raise ToolError("Binary document parsing is not available.")
            return text
        if name == "deliver_file":
            filename = arguments.get("filename", "")
            if not isinstance(filename, str) or "/" in filename or "\\" in filename or len(filename) > 200:
                raise ToolError("Use a single filename without directories, up to 200 characters.")
            if re.search(r'[<>:"|?*\x00-\x1f]', filename) or filename.endswith((".", " ")) or re.fullmatch(
                r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", filename, re.IGNORECASE
            ):
                raise ToolError("Use a portable filename without reserved device names or characters.")
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ToolError("Artifact content must be a UTF-8 text string.")
            data = content.encode("utf-8")
            if len(data) > self.config.max_upload_bytes:
                raise ToolError("Artifact exceeds the configured file size limit.")
            root = safe_root(self.config, session_id, "artifacts")
            path = scoped_path(root, filename)
            root.mkdir(parents=True, exist_ok=True)
            # The path has already been resolved; no user-controlled directory is created.
            path.write_bytes(data)
            return json.dumps({"filename": filename, "size": len(data), "download": f"/api/v1/sessions/{session_id}/artifacts/{filename}"}, ensure_ascii=False)
        if name == "todo_write":
            items = arguments.get("todos")
            if not isinstance(items, list) or len(items) > 50:
                raise ToolError("The checklist must contain at most 50 items.")
            normalized = []
            for item in items:
                if not isinstance(item, dict) or item.get("status") not in {"pending", "in_progress", "completed"}:
                    raise ToolError("Each checklist item needs a valid status.")
                content = str(item.get("content", "")).strip()
                if not content or len(content) > 1000:
                    raise ToolError("Checklist item text must contain 1 to 1000 characters.")
                normalized.append({"content": content, "status": item["status"]})
            if sum(item["status"] == "in_progress" for item in normalized) > 1:
                raise ToolError("At most one checklist item may be in progress.")
            return json.dumps({"todos": normalized}, ensure_ascii=False)
        if name == "ask_user_question":
            raise ToolError("Questions must be handled by the conversation runtime.")
        raise ToolError(f"Unknown conversation tool: {name}")
