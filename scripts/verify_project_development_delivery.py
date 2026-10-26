"""Run a real project feature and bug-fix task in an isolated source checkout.

The copied IDE search service, FastAPI routes and existing regression tests are
real repository files. External acceptance tests are immutable and initially
fail. No network/model request is made unless --live is explicitly supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
from collections import Counter
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from code_agent.models.events import EventType, ModelTurn, ToolCall
from code_agent.models.approval import executable_arguments
from code_agent.permissions.policy import BatchWriteFilePolicy, ContractGuardPolicy, Decision, PolicyResult, WriteFilePolicy
from code_agent.providers.offline import OfflineProvider
from code_agent.tools.builtin.tools_builtin import ReadFileTool
from code_agent.tools.builtin.code_tools import GrepTool
from scripts.verify_editing_delivery import SafeTranscript
from scripts.verify_long_editing_delivery import RecordingProvider


SOURCE = "code_agent/codeintel/file_search.py"
API = "code_agent/api/code_api.py"
NEW_TEST = "tests/test_search_whole_word.py"
BUG_TEST = "tests/test_search_glob.py"
ORIGINAL_TEST = "tests/test_project_file_search.py"
TARGETS = (SOURCE, API, NEW_TEST)
SCENARIOS = ("combined", "feature", "bug")
ACCEPTANCE_PATH = "../acceptance/test_search_delivery.py"
MARKER = "PROJECT_VERIFY "
PYTHON = Path(sys.executable).resolve().as_posix()


def scenario_targets(scenario="combined"):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown project delivery scenario")
    return (SOURCE, BUG_TEST) if scenario == "bug" else TARGETS


def scenario_test(scenario="combined"):
    return BUG_TEST if scenario == "bug" else NEW_TEST


ACCEPTANCE = r'''"""Immutable acceptance against the actual copied service and FastAPI app."""
from pathlib import Path
import sys
import pytest

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace"
sys.path.insert(0, str(WORKSPACE))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from code_agent.api.code_api import mount_code_api
from code_agent.api.security import create_token
from code_agent.codeintel import file_search
from code_agent.codeintel.projects import ProjectService
from code_agent.storage.db import Database, ProjectRepo, UserRepo


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "search-project"
    root.mkdir()
    values = {
        "root.py": "needle\n", "root.txt": "needle\n",
        "src/main.py": "needle\n", "src/main.js": "needle\n",
        "src/nested/deep.py": "needle\n", "src/nested/a.py": "needle\n",
        "src/nested/deeper/a.py": "needle\n", "src/other/b.py": "needle\n",
        "src/c.py": "needle\n", "src/x1.py": "needle\n",
        "src/x2.py": "needle\n", "source/direct.py": "needle\n",
        "unicode.txt": "😀cat Cat CAT cat_1 bobcat cat2 中文cat中文 cat!\n"
                       "a.*b xa.*by a.*b! foo.bar xfoo.barz foo.bar!\n"
                       "猫 猫咪 猫_1 😀猫!\n",
        ".env": "needle=secret\n", "secrets.json": '{"needle":"private"}',
        "dist/hidden.py": "needle\n",
    }
    for relative, text in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf8")
    return root


def paths(root, pattern):
    return {row["path"] for row in file_search.search_text(root, query="needle", include=pattern)["matches"]}


@pytest.mark.parametrize("pattern,expected", [
    ("src/*.py", {"src/main.py", "src/c.py", "src/x1.py", "src/x2.py"}),
    ("src/?.py", {"src/c.py"}),
    ("src/x?.py", {"src/x1.py", "src/x2.py"}),
    ("src/[cm]*.py", {"src/c.py", "src/main.py"}),
    ("src/**/main.py", {"src/main.py"}),
    ("src/**/deep.py", {"src/nested/deep.py"}),
    ("src/**/a.py", {"src/nested/a.py", "src/nested/deeper/a.py"}),
    ("src/*/a.py", {"src/nested/a.py"}),
    ("**/root.py", {"root.py"}),
    ("**/src/**/main.py", {"src/main.py"}),
    ("src/**/**/main.py", {"src/main.py"}),
    ("*.py", {"root.py"}),
    ("./src/*.py", {"src/main.py", "src/c.py", "src/x1.py", "src/x2.py"}),
    ("src\\*.py", {"src/main.py", "src/c.py", "src/x1.py", "src/x2.py"}),
    ("src/nested/*.py", {"src/nested/deep.py", "src/nested/a.py"}),
    ("source/*.py", {"source/direct.py"}),
    ("nonexistent/**/*.py", set()),
    ("src/[!c]*.py", {"src/main.py", "src/x1.py", "src/x2.py"}),
])
def test_path_segment_glob_semantics(project, pattern, expected):
    assert paths(project, pattern) == expected


@pytest.mark.parametrize("query,case_sensitive,expected", [
    ("cat", False, 4), ("cat", True, 2), ("Cat", True, 1), ("CAT", True, 1),
    ("a.*b", False, 2), ("foo.bar", False, 2), ("猫", False, 2),
])
def test_whole_word_unicode_literal_and_case(project, query, case_sensitive, expected):
    result = file_search.search_text(project, query=query, include="unicode.txt", whole_word=True,
                                   case_sensitive=case_sensitive)
    assert len(result["matches"]) == expected
    assert result["truncated"] is False


def test_default_remains_substring_and_utf16_columns(project):
    original = file_search.search_text(project, query="cat", include="unicode.txt")
    explicit = file_search.search_text(project, query="cat", include="unicode.txt", whole_word=False)
    assert original == explicit
    assert len(original["matches"]) == 8
    exact = file_search.search_text(project, query="cat", include="unicode.txt", whole_word=True)
    first = exact["matches"][0]
    assert first["column"] == 3 and first["end_column"] == 6
    assert first["text_start_column"] == 1
    assert first["text"].startswith("😀cat")


def test_whole_word_limit_reports_actual_truncation(project):
    first = file_search.search_text(project, query="cat", whole_word=True, limit=2)
    assert len(first["matches"]) == 2
    assert first["truncated"] and first["truncation_reason"] == "matches"
    exact = file_search.search_text(project, query="cat", whole_word=True, limit=4)
    assert len(exact["matches"]) == 4 and exact["truncated"] is False


@pytest.mark.parametrize("seed", range(6))
def test_whole_word_generated_inputs_are_not_fixed_examples(project, seed):
    import random
    rng = random.Random(9142 + seed)
    query = ["cat", "fox", "猫", "éclair", "foo.bar", "a.*b"][seed]
    fragments = [query, query.upper(), "pre" + query, query + "_1", "中" + query + "文",
                 "😀" + query + "!", "(" + query + ")", "zz", ""]
    text = " ".join(rng.choice(fragments) for _ in range(80))
    (project / "generated.txt").write_text(text, encoding="utf8")
    expected = []
    cursor = 0
    folded = text.lower()
    needle = query.lower()
    word = lambda char: char.isalnum() or char == "_"
    while True:
        start = folded.find(needle, cursor)
        if start < 0:
            break
        end = start + len(needle)
        if (start == 0 or not word(text[start - 1])) and (end == len(text) or not word(text[end])):
            expected.append((len(text[:start].encode("utf-16-le")) // 2 + 1,
                             len(text[:end].encode("utf-16-le")) // 2 + 1))
        cursor = end
    rows = file_search.search_text(project, query=query, include="generated.txt", whole_word=True)["matches"]
    assert [(row["column"], row["end_column"]) for row in rows] == expected


@pytest.mark.parametrize("include", ["../*", "/tmp/*", "C:/outside/*", "src/../../*", "src\x00*"])
def test_new_options_preserve_path_boundary(project, include):
    with pytest.raises(ValueError):
        file_search.search_text(project, query="needle", include=include, whole_word=True)


def test_copied_module_is_executed_and_hidden_files_stay_private(project):
    assert Path(file_search.__file__).resolve() == (WORKSPACE / "code_agent/codeintel/file_search.py").resolve()
    result = file_search.search_text(project, query="needle", include="**/*.py", whole_word=True)
    assert all(not row["path"].startswith("dist/") for row in result["matches"])
    result = file_search.search_text(project, query="needle", whole_word=True)
    assert all(row["path"] not in {".env", "secrets.json"} for row in result["matches"])


@pytest.fixture
def api(project, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_AGENT_AUTH_ENABLED", "true")
    monkeypatch.setenv("CODE_AGENT_HOME", str(tmp_path / "isolated-home"))
    db = Database(tmp_path / "api.db")
    owner = UserRepo(db).create("search-owner", "not-used", role="admin")
    other = UserRepo(db).create("search-other", "not-used")
    projects = ProjectRepo(db)
    entry = projects.create(owner["id"], "project", str(project), str(project))
    app = FastAPI()
    app.state.auth_enabled = True
    mount_code_api(app, project_repo=projects, manager=None, service=ProjectService(projects))
    def headers(user):
        return {"Authorization": "Bearer " + create_token(user["id"], user["username"], user["role"])}
    with TestClient(app) as client:
        yield client, entry["id"], headers(owner), headers(other)
    db.close()


def test_api_whole_word_reaches_service_and_matches_columns(api):
    client, pid, owner, _ = api
    base = f"/api/v1/code/projects/{pid}/search"
    exact = client.get(base, params={"query": "cat", "include": "unicode.txt", "whole_word": "true"}, headers=owner)
    assert exact.status_code == 200
    assert len(exact.json()["matches"]) == 4
    assert exact.json()["matches"][0]["column"] == 3
    normal = client.get(base, params={"query": "cat", "include": "unicode.txt"}, headers=owner)
    assert len(normal.json()["matches"]) == 8
    sensitive = client.get(base, params={"query": "cat", "whole_word": "true", "case_sensitive": "true"}, headers=owner)
    assert len(sensitive.json()["matches"]) == 2


def test_api_glob_semantics_and_whole_word_combination(api):
    client, pid, owner, _ = api
    response = client.get(f"/api/v1/code/projects/{pid}/search", params={
        "query": "needle", "include": "src/*.py", "whole_word": "true"}, headers=owner)
    assert response.status_code == 200
    assert {row["path"] for row in response.json()["matches"]} == {
        "src/main.py", "src/c.py", "src/x1.py", "src/x2.py"}


def test_api_schema_advertises_optional_boolean_default(api):
    client, _, _, _ = api
    route = client.get("/openapi.json").json()["paths"]["/api/v1/code/projects/{project_id}/search"]["get"]
    parameter = next(item for item in route["parameters"] if item["name"] == "whole_word")
    assert parameter["in"] == "query" and not parameter.get("required", False)
    assert parameter["schema"]["type"] == "boolean" and parameter["schema"]["default"] is False


@pytest.mark.parametrize("params", [
    {"query": "cat", "whole_word": "invalid"}, {"query": "cat", "whole_word": "true", "regex": "true"},
    {"query": "cat", "whole_word": "true", "include": "../*"},
    {"query": "cat", "whole_word": "true", "limit": 0},
])
def test_api_invalid_combinations_rejected(api, params):
    client, pid, owner, _ = api
    assert client.get(f"/api/v1/code/projects/{pid}/search", params=params, headers=owner).status_code == 422


def test_api_new_feature_preserves_auth_and_project_isolation(api):
    client, pid, owner, other = api
    url = f"/api/v1/code/projects/{pid}/search"
    params = {"query": "cat", "whole_word": "true"}
    assert client.get(url, params=params).status_code == 401
    assert client.get(url, params=params, headers=other).status_code == 404
    assert client.get(url.replace(pid, "missing"), params=params, headers=owner).status_code == 404
'''


BUG_ACCEPTANCE_EXTRA = r'''
@pytest.mark.parametrize("include", ["../*", "/tmp/*", "C:/outside/*", "src/../../*", "src\x00*"])
def test_glob_keeps_path_boundary(project, include):
    with pytest.raises(ValueError):
        file_search.search_text(project, query="needle", include=include)


def test_glob_keeps_copied_module_and_hidden_file_privacy(project):
    assert Path(file_search.__file__).resolve() == (WORKSPACE / "code_agent/codeintel/file_search.py").resolve()
    result = file_search.search_text(project, query="needle", include="**/*.py")
    assert all(not row["path"].startswith("dist/") for row in result["matches"])
    result = file_search.search_text(project, query="needle")
    assert all(row["path"] not in {".env", "secrets.json"} for row in result["matches"])


@pytest.mark.parametrize("pattern,expected", [
    ("src/*.py", {"src/main.py", "src/c.py", "src/x1.py", "src/x2.py"}),
    ("src/**/main.py", {"src/main.py"}),
    ("src/*/a.py", {"src/nested/a.py"}),
    ("**/root.py", {"root.py"}),
])
def test_api_glob_reaches_real_service(api, pattern, expected):
    client, pid, owner, _ = api
    response = client.get(f"/api/v1/code/projects/{pid}/search", params={
        "query": "needle", "include": pattern}, headers=owner)
    assert response.status_code == 200
    assert {row["path"] for row in response.json()["matches"]} == expected


def test_api_glob_preserves_auth_and_project_isolation(api):
    client, pid, owner, other = api
    url = f"/api/v1/code/projects/{pid}/search"
    params = {"query": "needle", "include": "src/*.py"}
    assert client.get(url, params=params).status_code == 401
    assert client.get(url, params=params, headers=other).status_code == 404
    assert client.get(url.replace(pid, "missing"), params=params, headers=owner).status_code == 404


@pytest.mark.parametrize("params", [
    {"query": "needle", "include": "../*"}, {"query": "needle", "include": "/tmp/*"},
    {"query": "needle", "regex": "true"}, {"query": "needle", "limit": 0},
])
def test_api_glob_preserves_invalid_query_rejection(api, params):
    client, pid, owner, _ = api
    assert client.get(f"/api/v1/code/projects/{pid}/search", params=params, headers=owner).status_code == 422
'''


def acceptance_source(scenario="combined"):
    """Keep the old combined benchmark byte-for-byte; split task obligations only.

    Function source and decorators are copied verbatim. Feature delivery does
    not have to repair the separate glob defect; bug delivery does not have to
    implement a new feature. These fixed suites are never generated by a model.
    """
    scenario_targets(scenario)
    if scenario == "combined":
        return ACCEPTANCE
    omitted = {"test_path_segment_glob_semantics", "test_api_glob_semantics_and_whole_word_combination"}
    tree = ast.parse(ACCEPTANCE)
    lines = ACCEPTANCE.splitlines(keepends=True)
    remove = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
            continue
        keep = node.name not in omitted if scenario == "feature" else node.name == "test_path_segment_glob_semantics"
        if not keep:
            first = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            remove.update(range(first - 1, node.end_lineno))
    source = "".join(line for index, line in enumerate(lines) if index not in remove)
    return source + (BUG_ACCEPTANCE_EXTRA if scenario == "bug" else "")


REFERENCE_TEST = '''"""Regression tests added by the offline plumbing provider only."""
from pathlib import Path
from code_agent.codeintel.file_search import search_text

def test_whole_word_feature(tmp_path):
    (tmp_path / "words.txt").write_text("cat bobcat cat_1 Cat!", encoding="utf8")
    assert len(search_text(tmp_path, query="cat", whole_word=True)["matches"]) == 2

def test_segment_glob_regression(tmp_path):
    (tmp_path / "src/deep").mkdir(parents=True)
    (tmp_path / "src/a.py").write_text("x", encoding="utf8")
    (tmp_path / "src/deep/b.py").write_text("x", encoding="utf8")
    assert [r["path"] for r in search_text(tmp_path, query="x", include="src/*.py")["matches"]] == ["src/a.py"]
'''


FEATURE_REFERENCE_TEST = '''from code_agent.codeintel.file_search import search_text

def test_whole_word_regression(tmp_path):
    (tmp_path / "words.txt").write_text("cat bobcat cat_1 Cat!", encoding="utf8")
    assert len(search_text(tmp_path, query="cat", whole_word=True)["matches"]) == 2

def test_default_substring_regression(tmp_path):
    (tmp_path / "words.txt").write_text("cat bobcat cat_1 Cat!", encoding="utf8")
    assert len(search_text(tmp_path, query="cat")["matches"]) == 4
'''


BUG_REFERENCE_TEST = '''from code_agent.codeintel.file_search import search_text

def test_direct_glob_regression(tmp_path):
    (tmp_path / "src/deep").mkdir(parents=True)
    (tmp_path / "src/a.py").write_text("x", encoding="utf8")
    (tmp_path / "src/deep/b.py").write_text("x", encoding="utf8")
    assert [r["path"] for r in search_text(tmp_path, query="x", include="src/*.py")["matches"]] == ["src/a.py"]

def test_recursive_glob_zero_levels_regression(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("x", encoding="utf8")
    assert [r["path"] for r in search_text(tmp_path, query="x", include="src/**/main.py")["matches"]] == ["src/main.py"]
'''


def snapshot(workspace):
    return {path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in workspace.rglob("*") if path.is_file()
            and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts
            and path.suffix != ".pyc"}


def make_fixture(root, *, scenario="combined"):
    targets = scenario_targets(scenario)
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    # Copy actual sources and contracts, never venv, user data, settings or keys.
    for source in (REPO / "code_agent").rglob("*"):
        if source.is_file() and (source.suffix == ".py" or source.name.endswith(".CONTRACT.md")):
            target = workspace / source.relative_to(REPO)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (workspace / "tests").mkdir()
    for relative in (ORIGINAL_TEST, "tests/conftest.py"):
        shutil.copy2(REPO / relative, workspace / relative)
    (root / "acceptance").mkdir()
    (root / "acceptance/test_search_delivery.py").write_text(acceptance_source(scenario), encoding="utf8")
    (root / "pytest.ini").write_text("[pytest]\naddopts =\n", encoding="utf8")
    (workspace / "README.md").write_text(
        "# Code-agent IDE project search\nThis workspace contains the real current repository's Python sources.\n"
        "Implement the requested task; preserve original regression tests.\n"
        "The external acceptance tests are readable but immutable. Writable paths: " + ", ".join(targets) + ".\n",
        encoding="utf8")
    initial = snapshot(workspace)
    (root / "source-manifest.json").write_text(json.dumps(initial, indent=2), encoding="utf8")
    return workspace, initial


def pytest_command(*, include_new=True, original_only=False, scenario="combined"):
    files = ([ORIGINAL_TEST] if original_only else [ACCEPTANCE_PATH, ORIGINAL_TEST])
    if include_new and not original_only:
        files.append(scenario_test(scenario))
    return shlex.quote(PYTHON) + " -m pytest -q --tb=short -c ../pytest.ini -o addopts= " + " ".join(files)


def external_verify(workspace, *, include_new=True, original_only=False, label="final", scenario="combined"):
    files = [ORIGINAL_TEST] if original_only else [ACCEPTANCE_PATH, ORIGINAL_TEST]
    if include_new and not original_only:
        files.append(scenario_test(scenario))
    report = workspace.parent / f"{label}-results.xml"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CODE_AGENT_HOME=str(workspace.parent / "verification-home"))
    started = time.monotonic()
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", "-c", "../pytest.ini", "-o", "addopts=",
                                *files, f"--junitxml={report}"], cwd=workspace, env=env, capture_output=True,
                               text=True, encoding="utf8", errors="replace", timeout=100)
    details = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "failing_tests": [], "suites": {}}
    if report.exists():
        xml = ET.parse(report).getroot()
        for case in xml.iter("testcase"):
            details["tests"] += 1
            group = "acceptance" if "test_search_delivery" in case.get("classname", "") else "new_tests" if Path(scenario_test(scenario)).stem in case.get("classname", "") else "existing_regression"
            details["suites"][group] = details["suites"].get(group, 0) + 1
            for kind in ("failure", "error", "skipped"):
                if case.find(kind) is not None:
                    details[{"failure": "failures", "error": "errors", "skipped": "skipped"}[kind]] += 1
                    if kind != "skipped":
                        details["failing_tests"].append({"name": case.get("name"), "message": case.find(kind).get("message", "")[:180]})
    return {"exit_code": completed.returncode, **details, "execution_seconds": round(time.monotonic() - started, 3),
            "output_tail": completed.stdout[-2200:] + completed.stderr[-600:]}


def _literal_command_path(raw, workspace):
    """Resolve one literal Bash/Windows spelling without shell expansion."""
    if not raw or any(char in raw for char in "\r\n\x00$`*?[]{}!"):
        return None
    # Git Bash displays Windows drive roots as /e/path; this is the same
    # literal directory as E:/path, not a different POSIX cwd.
    if os.name == "nt" and re.match(r"^/[A-Za-z]/", raw):
        raw = raw[1].upper() + ":/" + raw[3:]
    try:
        path = Path(raw)
        return (path if path.is_absolute() else Path(workspace) / path).resolve()
    except (OSError, ValueError):
        return None


def canonical_python_command(command, *, workspace=None):
    """One Python command, optionally preceded by literal cd to this workspace.

    Permission evaluation and final-suite detection share this parser. It
    never accepts pipelines, redirects or additional chained commands, so a
    successful final receipt cannot be produced by a masking command.
    """
    if not command.strip() or any(char in command for char in "\r\n\x00`$"):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split, lexer.commenters = True, ""
        parts = list(lexer)
    except ValueError:
        return None
    if parts and parts[0].lower() == "cd":
        if workspace is None or len(parts) < 5 or parts[2] != "&&":
            return None
        if _literal_command_path(parts[1], workspace) != Path(workspace).resolve():
            return None
        parts = parts[3:]
        if parts[1:3] != ["-m", "pytest"]:
            return None
    if any(re.fullmatch(r"[;&|<>]+", part) for part in parts):
        return None
    if len(parts) < 2 or Path(parts[0]).name.lower() not in {"python", "python.exe", "python3"}:
        return None
    return parts


def canonical_pytest_arguments(arguments, new_test=NEW_TEST, *, workspace=None):
    """Permit normal focused diagnosis without permitting test/plugin replacement.

    A filtered run is useful evidence during repair, but is explicitly excluded
    from the final full-suite delivery receipt below.
    """
    if not arguments:
        return None
    tests = {ACCEPTANCE_PATH, ORIGINAL_TEST, new_test}
    switches = {"-q", "-v", "-vv", "-x", "-l", "--showlocals", "--disable-warnings", "--collect-only",
                "--tb=short", "--tb=line", "--tb=no", "--tb=long", "--tb=auto"}
    index, selected = 0, False
    normalized = []
    while index < len(arguments):
        value = arguments[index]
        if value in switches or re.fullmatch(r"--maxfail=\d+", value):
            normalized.append(value)
            index += 1
            continue
        if value in {"-c", "-o", "-k", "--maxfail"}:
            if index + 1 >= len(arguments):
                return None
            option = arguments[index + 1]
            if value == "-c" and workspace is not None:
                config_path = _literal_command_path(option, workspace)
                if config_path == (Path(workspace) / "../pytest.ini").resolve():
                    option = "../pytest.ini"
            if value == "-c" and option != "../pytest.ini" or value == "-o" and option != "addopts=":
                return None
            if value == "-k" and (not option or not re.fullmatch(r"[\w .()\[\]*/?+:-]+", option)):
                return None
            if value == "--maxfail" and not option.isdigit():
                return None
            normalized.extend((value, option))
            index += 2
            continue
        filename, separator, selector = value.partition("::")
        canonical = filename if filename in tests else None
        if canonical is None and workspace is not None:
            path = _literal_command_path(filename, workspace)
            canonical = next((name for name in tests if path == (Path(workspace) / name).resolve()), None)
        if canonical is not None:
            selected = True
            normalized.append(canonical + (separator + selector if separator else ""))
            index += 1
            continue
        return None
    return normalized if selected else None


def safe_pytest_arguments(arguments, new_test=NEW_TEST, *, workspace=None):
    return canonical_pytest_arguments(arguments, new_test, workspace=workspace) is not None


def is_complete_project_check(command, *, new_test=NEW_TEST, workspace=None):
    parts = canonical_python_command(command, workspace=workspace)
    if parts is None or parts[1:3] != ["-m", "pytest"]:
        return False
    arguments = canonical_pytest_arguments(parts[3:], new_test, workspace=workspace)
    if arguments is None:
        return False
    if any(part in {"-k", "--collect-only"} or "::" in part for part in arguments):
        return False
    return {ACCEPTANCE_PATH, ORIGINAL_TEST, new_test} <= set(arguments)


class ProjectReadFileTool(ReadFileTool):
    """Fixture-only access to one immutable acceptance file, never its parent.

    Production tools retain their workspace boundary. A real developer can
    inspect the task's failing regression; this probe must not manufacture an
    unfixable permission error for the provided acceptance file.
    """
    def __init__(self, workspace):
        self.acceptance = (workspace / ACCEPTANCE_PATH).resolve()
        self.description = ReadFileTool.description + (
            f" This isolated task also permits read-only inspection of exactly {ACCEPTANCE_PATH} "
            f"(absolute path: {self.acceptance.as_posix()}). Its parent and sibling files remain outside the workspace boundary."
        )

    async def execute(self, arguments, ctx):
        raw = str(arguments.get("path") or "")
        path = Path(raw)
        resolved = (path if path.is_absolute() else ctx.workspace / path).resolve()
        if resolved == self.acceptance:
            return await super().execute({**arguments, "path": str(resolved)}, replace(ctx, workspace=self.acceptance.parent))
        return await super().execute(arguments, ctx)


class ProjectGrepTool(GrepTool):
    """The identical exact-file acceptance exception for native grep."""
    def __init__(self, workspace):
        self.acceptance = (workspace / ACCEPTANCE_PATH).resolve()
        self.description = GrepTool.description + (
            f" This isolated task also permits read-only search of exactly {ACCEPTANCE_PATH} "
            f"(absolute path: {self.acceptance.as_posix()}); do not search its parent directory."
        )

    async def execute(self, arguments, ctx):
        raw = str(arguments.get("path") or ".")
        path = Path(raw)
        resolved = (path if path.is_absolute() else ctx.workspace / path).resolve()
        if resolved == self.acceptance:
            result = await super().execute({**arguments, "path": str(resolved)}, replace(ctx, workspace=self.acceptance.parent))
            return result.replace(self.acceptance.name + ":", ACCEPTANCE_PATH + ":")
        return await super().execute(arguments, ctx)


def configure_probe_tools(registry, workspace, *, scenario="combined", shell_executions=None, edit_executions=None):
    """Describe only this probe's existing restrictions on its own instances."""
    registry.register(ProjectReadFileTool(workspace))
    registry.register(ProjectGrepTool(workspace))
    targets = ", ".join(scenario_targets(scenario))
    guidance = (
        " Isolated-project policy for THIS instance: use the existing absolute Python interpreter " + PYTHON +
        " for -m pytest against the supplied original/new/immutable acceptance suites (normal focused options such as "
        "-k, node selectors, -l/--showlocals and --tb=no are supported); -m py_compile for authorized targets; "
        "or -c with a bounded pathlib script referencing ONLY these literal writable paths: " + targets + ". "
        "Use native read_file/grep/list_dir/glob/git_read for source inspection and navigation, never shell ls/cat/head/grep. "
        "No background execution, pipelines, redirections, environment expansion or multiple chained commands. "
        "One optional literal cd to the CURRENT workspace followed by && and one Python pytest command is allowed; "
        "other directories and extra chains are denied. Final delivery requires all three supplied suites in one "
        "unfiltered pytest run; focused/collection runs are diagnosis only."
    )
    for name in ("bash", "bash_persistent"):
        tool = registry.get(name)
        if tool is not None:
            # A fresh registry belongs to this run. Do not alter a class-level
            # description, production registry or shared tool schema.
            tool.description = tool.description + guidance
            if shell_executions is not None:
                tool.execute = timed_shell_execute(name, tool.execute, shell_executions)
    if edit_executions is not None:
        for name in ("edit_file", "edit_many"):
            tool = registry.get(name)
            if tool is not None:
                tool.execute = measured_edit_execute(name, tool.execute, edit_executions)


def timed_shell_execute(name, execute, records):
    """Measure actual native execution, excluding approvals and observer hashing.

    The wrapper belongs only to a probe registry and returns/raises exactly the
    original result. It neither supplies evidence nor changes validation rules.
    """
    async def timed(arguments, ctx):
        started = time.monotonic()
        record = {"tool": name, "signature": json.dumps(executable_arguments(arguments), sort_keys=True, ensure_ascii=False)}
        try:
            result = await execute(arguments, ctx)
            record["outcome"] = "returned"
            if isinstance(result, str):
                match = re.match(r"exit_code=(-?\d+)\b", result)
                record["exit_code"] = int(match[1]) if match else None
                for count, outcome in re.findall(r"\b(\d+) (passed|failed|skipped|deselected|errors?)\b", result):
                    record["native_" + outcome.rstrip("s") + "_count"] = int(count)
            return result
        except BaseException as exc:
            record["outcome"] = type(exc).__name__
            raise
        finally:
            record["execution_seconds"] = round(time.monotonic() - started, 3)
            records.append(record)
    return timed


def _edit_signature(arguments):
    payload = json.dumps(executable_arguments(arguments), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf8")).hexdigest(), len(payload.encode("utf8"))


def edit_payload_details(name, arguments, workspace):
    """Measure exact payload against pre-execution source, never receipt prose.

    edit_many compares each operation with the same sequential in-memory state
    the native tool uses. The actual disk fingerprint is retained separately.
    This read does not grant FsObservation freshness or editing permission.
    """
    signature, argument_bytes = _edit_signature(arguments)
    entries = arguments.get("edits", []) if name == "edit_many" else [arguments]
    entries = entries if isinstance(entries, list) else []
    root, sources, operations = Path(workspace).resolve(), {}, []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("path") or "")
        old, new = str(entry.get("old_string") or ""), str(entry.get("new_string") or "")
        source = None
        try:
            path = Path(raw)
            path = (path if path.is_absolute() else root / path).resolve()
            path.relative_to(root)
            key = path.as_posix()
            if key not in sources:
                data = path.read_bytes()
                text = data.decode("utf8").replace("\r\n", "\n").replace("\r", "\n")
                sources[key] = {"disk_sha256": hashlib.sha256(data).hexdigest(), "disk_bytes": len(data), "text": text, "prior_operations": 0}
            source = sources[key]
        except (OSError, ValueError, UnicodeError):
            key = raw
        text = source["text"] if source is not None else None
        occurrences = text.count(old) if text is not None and old else 0
        whole = bool(text is not None and old and old == text)
        operation = {"path": raw, "old_utf8_bytes": len(old.encode("utf8")), "new_utf8_bytes": len(new.encode("utf8")),
                     "old_lines": len(old.splitlines()), "new_lines": len(new.splitlines()),
                     "source_available": source is not None, "matching_occurrences": occurrences,
                     "whole_file_replacement": whole}
        if source is not None:
            operation.update(source_disk_sha256=source["disk_sha256"], source_disk_bytes=source["disk_bytes"],
                             source_text_sha256=hashlib.sha256(text.encode("utf8")).hexdigest(),
                             source_text_bytes=len(text.encode("utf8")), source_text_lines=len(text.splitlines()),
                             source_state="batch_trial" if source["prior_operations"] else "disk_text")
            if old and old != new and occurrences and (occurrences == 1 or entry.get("replace_all")):
                source["text"] = text.replace(old, new, -1 if entry.get("replace_all") else 1)
                source["prior_operations"] += 1
        operations.append(operation)
    return {"tool": name, "signature": signature, "canonical_argument_utf8_bytes": argument_bytes,
            "old_utf8_bytes": sum(item["old_utf8_bytes"] for item in operations),
            "new_utf8_bytes": sum(item["new_utf8_bytes"] for item in operations),
            "old_lines": sum(item["old_lines"] for item in operations), "new_lines": sum(item["new_lines"] for item in operations),
            "operations": operations}


def measured_edit_execute(name, execute, records):
    """A probe-only transparent wrapper around the real native edit tool."""
    async def measured(arguments, ctx):
        record = edit_payload_details(name, arguments, ctx.workspace)
        try:
            result = await execute(arguments, ctx)
            record["outcome"] = "returned"
            return result
        except BaseException as exc:
            record["outcome"] = type(exc).__name__
            raise
        finally:
            records.append(record)
    return measured


class ProjectWritePolicy:
    def __init__(self, workspace, *, batch=False, scenario="combined"):
        self.workspace = workspace.resolve()
        self.targets = scenario_targets(scenario)
        self.batch = batch
        self.inner = BatchWriteFilePolicy(workspace, contract_guard=True) if batch else ContractGuardPolicy(WriteFilePolicy(workspace))

    def evaluate(self, arguments, ctx):
        result = self.inner.evaluate(arguments, ctx)
        if result.decision != Decision.allow:
            return result
        edits = arguments.get("edits", []) if self.batch else [arguments]
        for edit in edits:
            path = Path(edit["path"])
            try:
                relative = (path if path.is_absolute() else self.workspace / path).resolve().relative_to(self.workspace).as_posix()
            except (ValueError, OSError):
                return PolicyResult(Decision.deny, "只允许隔离工作区内授权实现和新增测试")
            if relative not in self.targets:
                return PolicyResult(Decision.deny, "原有测试、外部验收和其它生产模块为只读")
        return result


class ProjectShellPolicy:
    """Allow actual project pytest runs, plus bounded Python edits of targets.

    Native edits remain unrestricted in shape. Python scripts may use pathlib
    for the three target files only; no exec/eval/importlib/process/network or
    test/config replacement through the shell.
    """
    def __init__(self, *, scenario="combined"):
        self.targets = scenario_targets(scenario)
        self.test = scenario_test(scenario)

    def evaluate(self, arguments, ctx):
        command = str(arguments.get("command") or "").strip()
        if arguments.get("run_in_background") or any(ch in command for ch in "\r\n`$"):
            return PolicyResult(Decision.deny, "请单独运行本地校验或限定目标脚本")
        parts = canonical_python_command(command, workspace=ctx.workspace)
        if parts is None:
            return PolicyResult(Decision.deny, "仅允许单条现成Python校验/限定脚本，或字面cd到当前工作区后接一条pytest；不允许管道、重定向或其它串接")
        if parts[1:3] == ["-m", "pytest"]:
            if not safe_pytest_arguments(parts[3:], self.test, workspace=ctx.workspace):
                return PolicyResult(Decision.deny, "仅运行原有测试、授权新增测试和不可改外部验收；支持定点筛选，勿更换配置或插件")
            return PolicyResult(Decision.allow, "隔离项目的真实 pytest 校验")
        if parts[1:3] == ["-m", "py_compile"] and parts[3:] and set(parts[3:]) <= set(self.targets):
            return PolicyResult(Decision.allow, "授权实现的语法检查")
        if parts[1] == "-c" and len(parts) == 3:
            return self._script(parts[2])
        return PolicyResult(Decision.deny, "请使用文档中的真实 pytest 校验命令或原生编辑工具")

    def _script(self, code):
        import ast

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return PolicyResult(Decision.deny, "脚本语法不完整")
        allowed_methods = {"Path", "read_text", "write_text", "replace", "count", "index", "find", "startswith", "endswith", "join", "split", "splitlines"}
        def literal_path(node):
            return (isinstance(node, ast.Call)
                    and (isinstance(node.func, ast.Name) and node.func.id == "Path"
                         or isinstance(node.func, ast.Attribute) and node.func.attr == "Path")
                    and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in self.targets)

        path_bindings = {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                         and literal_path(node.value) for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name != "pathlib" for alias in node.names):
                return PolicyResult(Decision.deny, "批改脚本仅允许 pathlib")
            if isinstance(node, ast.ImportFrom) and (node.module != "pathlib" or any(alias.name != "Path" for alias in node.names)):
                return PolicyResult(Decision.deny, "批改脚本仅允许 pathlib.Path")
            if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr not in allowed_methods):
                return PolicyResult(Decision.deny, "脚本不可遍历目录、启动进程或动态执行")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id not in {"Path", "str", "print", "len"}:
                    return PolicyResult(Decision.deny, "使用原生编辑，或仅对授权显式路径作 Path 读写")
                if (isinstance(node.func, ast.Name) and node.func.id == "Path"
                        or isinstance(node.func, ast.Attribute) and node.func.attr == "Path"):
                    if not literal_path(node):
                        return PolicyResult(Decision.deny, "Path 必须是授权的三个显式目标之一")
                if isinstance(node.func, ast.Attribute):
                    receiver = node.func.value
                    path_receiver = literal_path(receiver) or isinstance(receiver, ast.Name) and receiver.id in path_bindings
                    if node.func.attr in {"read_text", "write_text"} and not path_receiver:
                        return PolicyResult(Decision.deny, "文件读写只能通过授权的显式 Path")
                    if node.func.attr == "replace" and path_receiver:
                        return PolicyResult(Decision.deny, "不可使用 Path.replace 移动文件")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.With, ast.Try)):
                return PolicyResult(Decision.deny, "批改脚本无需定义函数或动态执行结构")
        return PolicyResult(Decision.allow, "限定目标路径的本地 Python 批改")


def reference_edits(workspace, *, scenario="combined"):
    """Deterministic offline solution validates plumbing, never supplies live edits."""
    source = (workspace / SOURCE).read_text(encoding="utf8")
    start = source.index("def _matches_include(")
    end = source.index("\n\ndef _inventory", start)
    replacement = '''def _matches_include(path: str, pattern: str) -> bool:
    if not pattern:
        return True
    path_parts, pattern_parts = path.split("/"), pattern.split("/")
    cache = {}
    def match(i, j):
        if (i, j) in cache:
            return cache[i, j]
        if j == len(pattern_parts):
            result = i == len(path_parts)
        elif pattern_parts[j] == "**":
            result = match(i, j + 1) or (i < len(path_parts) and match(i + 1, j))
        else:
            result = i < len(path_parts) and fnmatch.fnmatchcase(path_parts[i], pattern_parts[j]) and match(i + 1, j + 1)
        cache[i, j] = result
        return result
    return match(0, 0)
'''
    if scenario != "feature":
        source = source[:start] + replacement + source[end:]
    if scenario == "bug":
        return {SOURCE: source, BUG_TEST: BUG_REFERENCE_TEST}
    source = source.replace("include: str = \"\", limit: int = 200,", "include: str = \"\", limit: int = 200, whole_word: bool = False,")
    source = source.replace("needle = re.compile(re.escape(query), flags=0 if case_sensitive else re.IGNORECASE)",
                            "expression = re.escape(query)\n    if whole_word:\n        expression = r\"(?<!\\w)\" + expression + r\"(?!\\w)\"\n    needle = re.compile(expression, flags=0 if case_sensitive else re.IGNORECASE)")
    api = (workspace / API).read_text(encoding="utf8")
    api = api.replace("        case_sensitive: bool = False,\n        regex: bool = False,", "        case_sensitive: bool = False,\n        whole_word: bool = False,\n        regex: bool = False,")
    api = api.replace("regex=regex, include=include, limit=limit,", "regex=regex, include=include, limit=limit, whole_word=whole_word,")
    return {SOURCE: source, API: api, NEW_TEST: FEATURE_REFERENCE_TEST if scenario == "feature" else REFERENCE_TEST}


def offline_provider(workspace, *, scenario="combined"):
    script = [OfflineProvider.tool_call_turn("bash", {"command": pytest_command(include_new=False, scenario=scenario), "timeout": 100})]
    reads = [ToolCall(name="read_file", arguments={"path": SOURCE})]
    if scenario != "bug":
        reads.append(ToolCall(name="read_file", arguments={"path": API, "offset": 445, "limit": 45}))
    script.append(ModelTurn(tool_calls=reads, stop_reason="tool_use"))
    for path, value in reference_edits(workspace, scenario=scenario).items():
        script.append(OfflineProvider.tool_call_turn("write_file", {"path": path, "content": value}))
    script.append(OfflineProvider.tool_call_turn("bash", {"command": pytest_command(scenario=scenario), "timeout": 100}))
    script.append(OfflineProvider.text_turn("已实现全词搜索并修复路径 glob，新增测试且真实 API 与原有回归通过。"))
    return OfflineProvider(script=script)


def task_prompt(scenario="combined"):
    if scenario != "combined":
        return focused_task_prompt(scenario)
    return (
        "请在当前真实 Code-agent 项目的 IDE 文件搜索中完整实现功能并修复 bug，实际修改源码、补测试并验证。\n"
        "1. 新增 whole_word 全词搜索选项，默认 false 保持现有子串行为；FastAPI 搜索接口可接收该布尔参数并传到真实搜索服务。"
        "全词边界按 Unicode 字母/数字/下划线判定，中文也属于词字符；匹配内容仍为字面量，遵守 case_sensitive；"
        "保留 Monaco UTF-16 列、分页截断、文件安全边界和现有 regex 禁用行为。\n"
        "2. 修复 include 的路径 glob：src/*.py 当前错误匹配 src/nested/deep.py，src/**/main.py 漏掉 src/main.py。"
        "星号、问号、字符类只匹配单个路径段，独立 ** 路径段匹配零层或多层目录；兼容根文件、重复 ** 和规范化相对路径。\n"
        "\n可写范围：只允许修改 code_agent/codeintel/file_search.py、code_agent/api/code_api.py、tests/test_search_whole_word.py。\n"
        "只读参考：tests/test_project_file_search.py、tests/conftest.py、../acceptance/test_search_delivery.py、当前工程其它依赖和契约。"
        "这些只读文件均不可修改；外部验收可用原生 read_file 或 grep 读取精确文件路径。\n"
        "新测试至少覆盖 service 全词和 glob 回归，不能通过跳过/改断言/替换依赖伪造通过。\n"
        "先运行失败用例定位，再按小批次读、改、验证；无须通读整个项目。当前3个授权文件可作为一批，保留准确进度，"
        "不要在已有源码和锚点后反复读取相同全文。可使用原生编辑或限定目标的 pathlib Python 脚本，"
        "所有 shell 调用使用下列现成虚拟环境解释器，系统 python 未安装依赖。\n"
        "新增测试前，完整基线命令：" + pytest_command(include_new=False) + "\n"
        "交付必须实际运行原有回归+新增测试+不可改外部验收，完整命令：" + pytest_command() + "\n"
        "提供工具审批说明 _approval 的 purpose 和 effects。完成后简要报告修复、功能、真实测试数量。"
    )


def focused_task_prompt(scenario):
    targets = scenario_targets(scenario)
    request = (
        "请在当前真实 Code-agent 项目的 IDE 文件搜索中开发 whole_word 全词搜索，实际改源码、补回归并验证。"
        "默认 false 保持原有子串行为；FastAPI 搜索接口接受布尔参数并传入真实服务。"
        "边界使用 Unicode 字母/数字/下划线，中文属于词字符，查询仍为字面量且遵守 case_sensitive。"
        "保留 UTF-16 Monaco 列、截断限制、regex 禁用及安全边界。include glob 的既有缺陷不属于本任务，不必修复。"
        if scenario == "feature" else
        "请修复当前真实 Code-agent 项目 IDE 文件搜索的 include glob bug，实际改源码、补回归并验证。"
        "复现：src/*.py 错误匹配 src/nested/deep.py，src/**/main.py 漏掉 src/main.py。"
        "星号、问号、字符类只匹配单个路径段，独立 ** 路径段匹配零层或多层目录；"
        "兼容根文件、重复 **、规范化相对路径，保留安全边界、性能限制与现有搜索/API行为。"
        "本任务不开发全词功能，不需修改API模块。"
    )
    return (
        request + "\n可写范围：只允许修改 " + "、".join(targets) + "。\n"
        "只读参考：tests/test_project_file_search.py、tests/conftest.py、../acceptance/test_search_delivery.py、当前工程其它依赖和契约。"
        "这些只读文件均不可修改；外部验收可用原生 read_file 或 grep 读取精确文件路径。\n"
        "新增至少两项实际行为回归测试。"
        "按小批次读、改、验证，复用现有源码和锚点，无须通读工程。"
        "使用原生编辑或限定授权显式路径的 pathlib Python脚本；所有Shell调用用下面的现成虚拟环境Python，"
        "支持pytest -k、节点选择器、--tb=no做定点检查，但交付前必须运行不筛选的完整套件。"
        "新增测试前的完整基线命令：" + pytest_command(include_new=False, scenario=scenario) + "\n"
        "完整交付验证命令：" + pytest_command(scenario=scenario) + "\n"
        "工具调用提供 _approval 的 purpose 和 effects；完成后简要报告修改与真实测试数量。"
    )


class ProjectRecordingProvider(RecordingProvider):
    """Record real request latency and provider token usage without prompt text."""
    def _finish_request(self, started, index, turn=None):
        if index >= len(self.request_summaries):
            return  # Budget rejection made no provider request.
        summary = self.request_summaries[index]
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if turn is not None:
            summary["usage"] = turn.usage.model_dump()
            summary["stop_reason"] = turn.stop_reason

    async def chat(self, messages, tools):
        started, turn, index = time.monotonic(), None, len(self.request_summaries)
        try:
            turn = await super().chat(messages, tools)
            return turn
        finally:
            self._finish_request(started, index, turn)

    async def stream(self, messages, tools):
        started, turn, index = time.monotonic(), None, len(self.request_summaries)
        try:
            async for chunk in super().stream(messages, tools):
                if chunk.turn is not None:
                    turn = chunk.turn
                yield chunk
        finally:
            self._finish_request(started, index, turn)


def pytest_run_details(command, *, workspace, new_test=NEW_TEST):
    """Classify diagnostic cost; this never grants a successful test receipt."""
    parts = canonical_python_command(command, workspace=workspace)
    if parts is None or parts[1:3] != ["-m", "pytest"]:
        return None
    arguments = canonical_pytest_arguments(parts[3:], new_test, workspace=workspace)
    if arguments is None:
        return None
    allowed = {ACCEPTANCE_PATH, ORIGINAL_TEST, new_test}
    suites = sorted({part.split("::", 1)[0] for part in arguments if part.split("::", 1)[0] in allowed})
    filtered = "-k" in arguments or any("::" in part for part in arguments)
    if "--collect-only" in arguments:
        kind = "collection"
    elif filtered:
        kind = "focused"
    elif allowed <= set(suites):
        kind = "full_project"
    elif {ACCEPTANCE_PATH, ORIGINAL_TEST} <= set(suites):
        kind = "baseline_full"
    else:
        kind = "focused"
    return {"kind": kind, "suites": suites, "filtered": filtered}


def test_workflow_metrics(steps, *, workspace, new_test=NEW_TEST):
    """Summarize test scheduling using actual versions, including saved old runs.

    A required initial failing baseline is reported separately from an early
    post-edit full run. New versions invalidate duplicate-run comparisons.
    Failed tests are still executions; denied calls are not executions.
    """
    timeline, seen_versions = [], set()
    repeated_full = premature_full = 0
    created_after_full = rewritten_after_full = 0
    created_after_success = rewritten_after_success = 0
    changes_after_success = source_changes_after_success = 0
    full_seen = successful_full_seen = implementation_changed = False
    previous_versions = {}
    for index, step in enumerate(steps):
        changed = step.get("changed", [])
        versions = step.get("versions", {})
        if successful_full_seen and any(path in (SOURCE, API, new_test) for path in changed):
            changes_after_success += 1
            source_changes_after_success += int(any(path in (SOURCE, API) for path in changed))
        if new_test in changed:
            created = new_test in step.get("created_paths", []) or not previous_versions.get(new_test)
            if full_seen:
                created_after_full += int(created)
                rewritten_after_full += int(not created)
            if successful_full_seen:
                created_after_success += int(created)
                rewritten_after_success += int(not created)
        if any(path in changed for path in (SOURCE, API)):
            implementation_changed = True
        previous_versions = versions
        if step.get("tool") not in {"bash", "bash_persistent"}:
            continue
        details = pytest_run_details(str(step.get("arguments", {}).get("command", "")), workspace=workspace, new_test=new_test)
        executed = step.get("shell_executed", not step.get("error", False)) and not step.get("deferred", False)
        if details is None or not executed:
            continue
        seconds = step.get("shell_execution_seconds")
        timing_source = "native_execution"
        if seconds is None:
            seconds, timing_source = step.get("tool_seconds", 0), "tool_event_legacy"
        broad = details["kind"] in {"full_project", "baseline_full"}
        version = step.get("workspace_version") or hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
        signature = (tuple(details["suites"]), version)
        duplicate = broad and signature in seen_versions
        early = broad and implementation_changed and not versions.get(new_test)
        superseded = broad and implementation_changed and any(
            any(path in (SOURCE, API, new_test) for path in later.get("changed", [])) for later in steps[index + 1:])
        if broad:
            repeated_full += int(duplicate)
            premature_full += int(early)
            seen_versions.add(signature)
            full_seen = True
            successful_full_seen = successful_full_seen or step.get("successful_test", False)
        timeline.append({"turn": step.get("turn"), "kind": details["kind"], "suites": details["suites"],
                         "seconds": seconds, "timing_source": timing_source,
                         "successful": step.get("successful_test", False), "passed_tests": step.get("passed_tests", 0),
                         "exit_code": step.get("shell_exit_code"),
                         "same_version_repeated_full": bool(duplicate), "full_before_regression_creation": bool(early),
                         "post_edit_full_superseded_by_later_writes": bool(superseded)})
    counts = Counter(row["kind"] for row in timeline)
    return {"pytest_run_count": len(timeline), "full_project_test_runs": counts["full_project"],
            "baseline_full_test_runs": counts["baseline_full"], "focused_test_runs": counts["focused"],
            "collection_test_runs": counts["collection"],
            "pytest_execution_seconds": round(sum(row["seconds"] for row in timeline), 3),
            "full_test_execution_seconds": round(sum(row["seconds"] for row in timeline if row["kind"] in {"full_project", "baseline_full"}), 3),
            "focused_test_execution_seconds": round(sum(row["seconds"] for row in timeline if row["kind"] == "focused"), 3),
            "same_version_repeated_full_test_runs": repeated_full,
            "post_edit_full_before_regression_creation": premature_full,
            "test_creations_after_full": created_after_full, "test_rewrites_after_full": rewritten_after_full,
            "test_creations_after_successful_full": created_after_success,
            "test_rewrites_after_successful_full": rewritten_after_success,
            "target_change_calls_after_successful_full": changes_after_success,
            "implementation_change_calls_after_successful_full": source_changes_after_success,
            "post_edit_full_runs_superseded_by_later_writes": sum(row["post_edit_full_superseded_by_later_writes"] for row in timeline),
            "test_run_timeline": timeline}


def edit_workflow_metrics(steps):
    measured = [step["edit_payload"] for step in steps if step.get("edit_executed") and step.get("edit_payload")]
    operations = [operation for call in measured for operation in call["operations"]]
    successful = [call for call in measured if call.get("outcome") == "returned"]
    return {"native_edit_execution_count": len(measured), "native_edit_operation_count": len(operations),
            "edit_old_utf8_bytes": sum(call["old_utf8_bytes"] for call in measured),
            "edit_new_utf8_bytes": sum(call["new_utf8_bytes"] for call in measured),
            "edit_old_lines": sum(call["old_lines"] for call in measured),
            "edit_new_lines": sum(call["new_lines"] for call in measured),
            "max_edit_call_replacement_utf8_bytes": max((call["old_utf8_bytes"] + call["new_utf8_bytes"] for call in measured), default=0),
            "max_edit_operation_replacement_utf8_bytes": max((operation["old_utf8_bytes"] + operation["new_utf8_bytes"] for operation in operations), default=0),
            "max_edit_canonical_argument_utf8_bytes": max((call["canonical_argument_utf8_bytes"] for call in measured), default=0),
            "whole_file_replacement_attempts": sum(operation["whole_file_replacement"] for operation in operations),
            "successful_whole_file_replacement_operations": sum(operation["whole_file_replacement"] for call in successful for operation in call["operations"]),
            "edit_execution_timeline": [{"turn": step["turn"], "tool": step["tool"], **step["edit_payload"]}
                                        for step in steps if step.get("edit_executed") and step.get("edit_payload")]}


def post_delivery_metrics(steps, *, model_calls, current_versions=None):
    """Count from the first successful receipt of the final current versions."""
    if current_versions is None:
        current_versions = steps[-1].get("versions", {}) if steps else {}
    receipts = [(index, step) for index, step in enumerate(steps)
                if step.get("complete_project_test") and step.get("versions") == current_versions]
    if not receipts:
        return {"post_delivery_readonly_tool_calls": None, "post_delivery_read_file_calls": None,
                "post_delivery_grep_calls": None, "post_delivery_model_turns": None}
    index, receipt = receipts[0]
    trailing = steps[index + 1:]
    readonly = {"read_file", "grep", "glob", "list_dir", "code_map", "code_search", "code_features", "code_graph", "git_read"}
    return {"post_delivery_readonly_tool_calls": sum(step.get("tool") in readonly for step in trailing),
            "post_delivery_read_file_calls": sum(step.get("tool") == "read_file" for step in trailing),
            "post_delivery_grep_calls": sum(step.get("tool") == "grep" for step in trailing),
            "post_delivery_model_turns": model_calls - receipt["turn"]}


class ProjectObserver:
    def __init__(self, workspace, provider, initial, *, scenario="combined", minimum_tests=0):
        self.workspace, self.provider = workspace, provider
        self.initial = initial
        self.before = dict(initial)
        self.steps, self.calls = [], {}
        self.targets, self.new_test = scenario_targets(scenario), scenario_test(scenario)
        self.minimum_tests = minimum_tests
        self.started = time.monotonic()
        self.call_started, self.read_coverage, self.queries = {}, {}, set()
        self.shell_executions = []
        self.edit_executions = []

    def _edit_metrics(self, name, arguments):
        if name not in {"edit_file", "edit_many"}:
            return {}
        signature, _ = _edit_signature(arguments)
        record = next((record for record in self.edit_executions
                       if record["tool"] == name and record["signature"] == signature and not record.get("claimed")), None)
        if record is None:
            return {"edit_executed": False}
        record["claimed"] = True
        return {"edit_executed": True,
                "edit_payload": {key: value for key, value in record.items() if key not in {"signature", "claimed", "tool"}}}

    def _shell_metrics(self, name, arguments):
        if name not in {"bash", "bash_persistent"}:
            return {}
        signature = json.dumps(executable_arguments(arguments), sort_keys=True, ensure_ascii=False)
        record = next((record for record in self.shell_executions
                       if record["tool"] == name and record["signature"] == signature and not record.get("claimed")), None)
        if record is None:
            return {"shell_executed": False, "shell_execution_seconds": None, "shell_exit_code": None}
        record["claimed"] = True
        return {"shell_executed": True, "shell_execution_seconds": record["execution_seconds"],
                "shell_exit_code": record.get("exit_code"), "shell_outcome": record["outcome"],
                **{key: value for key, value in record.items() if key.startswith("native_")}}

    def _query_metrics(self, name, arguments, after, result, error):
        metrics = {"repeated_query": False, "repeated_read": False, "read_lines": 0, "repeated_read_lines": 0}
        if name not in {"read_file", "grep", "code_search", "code_map", "list_dir", "glob"}:
            return metrics
        args = executable_arguments(arguments)
        raw = str(args.get("path") or ".")
        path = Path(raw)
        path = (path if path.is_absolute() else self.workspace / path).resolve()
        try:
            normalized = path.relative_to(self.workspace).as_posix()
        except ValueError:
            normalized = path.as_posix()
        args = {**args, "path": normalized}
        try:
            version = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else hashlib.sha256(
                json.dumps({p: v for p, v in after.items() if normalized == "." or p.startswith(normalized + "/")}, sort_keys=True).encode()).hexdigest()
        except OSError:
            version = "missing"
        signature = (name, json.dumps(args, sort_keys=True, ensure_ascii=False), version)
        metrics["repeated_query"] = signature in self.queries
        # A refused read does not create reusable source evidence.
        if not error:
            self.queries.add(signature)
        if name == "read_file" and not error and path.is_file():
            total = len(path.read_text(encoding="utf8", errors="replace").splitlines())
            start = max(1, int(args.get("offset") or 1))
            length = min(int(args.get("limit") or 200), 2000) if args.get("offset") is not None else total
            lines = set(range(start, min(start + length, total + 1)))
            coverage = self.read_coverage.setdefault((normalized, version), set())
            metrics["read_lines"] = len(lines)
            metrics["repeated_read_lines"] = len(lines & coverage)
            metrics["repeated_read"] = bool(lines) and lines <= coverage
            coverage.update(lines)
        return metrics

    def on_event(self, event):
        if event.type == EventType.tool_use:
            call = event.payload["call"]
            self.calls[call["id"]] = call
            self.call_started[call["id"]] = time.monotonic()
        elif event.type == EventType.tool_result:
            after = snapshot(self.workspace)
            changed = sorted(path for path in set(after) | set(self.before) if after.get(path) != self.before.get(path))
            call = self.calls.get(event.payload["call_id"], {})
            result = str(event.payload.get("result", ""))
            name = event.payload.get("name")
            arguments = call.get("arguments", {})
            full_check = is_complete_project_check(str(arguments.get("command", "")), new_test=self.new_test, workspace=self.workspace)
            test_count = max((int(value) for value in re.findall(r"\b(\d+) passed\b", result)), default=0)
            error = result.startswith(("[tool error]", "[permission denied]", "[任务推进]"))
            deferred = result.startswith("[tool deferred]")
            command_parts = canonical_python_command(str(arguments.get("command", "")), workspace=self.workspace)
            deferred_validation = deferred and (
                "[verification plan]" in result[:120] or
                command_parts is not None and command_parts[1:3] in (["-m", "pytest"], ["-m", "py_compile"]))
            complete = (full_check and result.startswith("exit_code=0") and test_count >= self.minimum_tests
                        and test_count > 0 and not re.search(r"\b[1-9]\d* (?:skipped|deselected)\b", result))
            self.steps.append({"turn": self.provider.calls, "tool": event.payload.get("name"), "arguments": call.get("arguments", {}),
                               "changed": changed, "error": error,
                               "deferred": deferred, "deferred_validation": bool(deferred_validation),
                               "created_paths": [path for path in changed if not self.before.get(path) and after.get(path)],
                               "workspace_version": hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(),
                               "successful_test": result.startswith("exit_code=0") and " passed" in result,
                               "complete_project_test": complete, "passed_tests": test_count,
                               "permission_error": result.startswith("[permission denied]"),
                               "task_discipline_block": "[任务推进]" in result[:100],
                               "read_error": name == "read_file" and error,
                               "elapsed_seconds": round(time.monotonic() - self.started, 3),
                               "tool_seconds": round(time.monotonic() - self.call_started.get(event.payload["call_id"], time.monotonic()), 3),
                               **self._shell_metrics(name, arguments),
                               **self._edit_metrics(name, arguments),
                               **self._query_metrics(name, arguments, after, result, error),
                               "result_tail": result[-2500:], "versions": {p: after.get(p) for p in self.targets}})
            self.before = after

    def summary(self):
        writes = [s for s in self.steps if s["changed"]]
        passed_tests = [s for s in self.steps if s["complete_project_test"]]
        model_verified = passed_tests[-1]["versions"] if passed_tests else {}
        changed = sorted({p for s in writes for p in s["changed"]})
        by_turn = {}
        for step in self.steps:
            by_turn.setdefault(step["turn"], []).append(step)
        longest = current = 0
        for steps in by_turn.values():
            if any(s["changed"] or s["successful_test"] for s in steps):
                current = 0
            else:
                current += 1
                longest = max(longest, current)
        first_write = min((s["turn"] for s in writes), default=None)
        requests = getattr(self.provider, "request_summaries", [])
        shell_steps = [step for step in self.steps if step.get("shell_executed")]
        return {"tool_counts": dict(Counter(s["tool"] for s in self.steps)), "first_write_turn": first_write,
                "tool_call_count": len(self.steps), "read_call_count": sum(s["tool"] == "read_file" for s in self.steps),
                "query_call_count": sum(s["tool"] in {"grep", "code_search", "code_map", "list_dir", "glob"} for s in self.steps),
                "longest_readonly_turn_streak": longest, "changed_paths": changed,
                "out_of_scope_changes": [p for p in changed if p not in self.targets],
                "model_verified_paths": [p for p in self.targets if self.before.get(p) and model_verified.get(p) == self.before.get(p)],
                "successful_model_test_runs": len(passed_tests), "tool_error_count": sum(s["error"] for s in self.steps),
                "deferred_tool_count": sum(s["deferred"] for s in self.steps),
                "deferred_validation_count": sum(s["deferred_validation"] for s in self.steps),
                "permission_error_count": sum(s["permission_error"] for s in self.steps),
                "task_discipline_block_count": sum(s["task_discipline_block"] for s in self.steps),
                "read_error_count": sum(s["read_error"] for s in self.steps),
                "repeated_query_count": sum(s["repeated_query"] for s in self.steps),
                "repeated_read_count": sum(s["repeated_read"] for s in self.steps),
                "read_lines": sum(s["read_lines"] for s in self.steps),
                "repeated_read_lines": sum(s["repeated_read_lines"] for s in self.steps),
                "first_write_seconds": min((s["elapsed_seconds"] for s in writes), default=None),
                "model_tokens_through_first_write": {key: sum(request.get("usage", {}).get(key, 0) for request in requests
                                                               if first_write is not None and request["turn"] <= first_write)
                                                     for key in ("input_tokens", "cached_tokens", "output_tokens")},
                "model_turns_after_delivery_verification": self.provider.calls - passed_tests[-1]["turn"] if passed_tests else None,
                "shell_execution_count": len(self.shell_executions),
                "shell_execution_seconds": round(sum(record["execution_seconds"] for record in self.shell_executions), 3),
                "shell_execution_timeline": [{"turn": step["turn"], "tool": step["tool"], "seconds": step["shell_execution_seconds"],
                                              "exit_code": step["shell_exit_code"], "outcome": step["shell_outcome"]} for step in shell_steps],
                "unreported_shell_executions": sum(not record.get("claimed", False) for record in self.shell_executions),
                **test_workflow_metrics(self.steps, workspace=self.workspace, new_test=self.new_test),
                **edit_workflow_metrics(self.steps),
                **post_delivery_metrics(self.steps, model_calls=self.provider.calls,
                                        current_versions={path: self.before.get(path) for path in self.targets})}


def resolve_probe_provider(workspace, *, live=False, profile_id=None, scenario="combined"):
    """Capture one existing profile without activating it or changing defaults.

    Read the raw profile file deliberately: settings._read_profiles performs
    legacy cleanup writes, which are inappropriate for an isolated probe.
    """
    from code_agent.agent.router import KNOWN_PROVIDERS, ModelRouter

    if profile_id and not live:
        raise ValueError("--profile-id requires --live")
    if profile_id:
        from code_agent import settings
        from code_agent.providers import create_provider
        from code_agent.providers.deepseek import DeepSeekProvider
        from code_agent.providers.openai_compat import OpenAICompatProvider

        try:
            stored = json.loads(settings._profiles_file().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ValueError("Existing model profile file is unavailable") from None
        profiles = stored.get("profiles", []) if isinstance(stored, dict) else []
        selected = next((p for p in profiles if isinstance(p, dict) and p.get("id") == profile_id), None)
        if selected is None:
            raise ValueError("Requested existing model profile was not found")
        name = str(selected.get("provider") or "").strip()
        if name not in KNOWN_PROVIDERS or name == "offline":
            raise ValueError("Live profile needs a configured non-offline provider")
        key = str(selected.get("api_key") or "").strip()
        if not key:
            raise ValueError("Existing live profile has no API key")
        provider_class = DeepSeekProvider if name == "deepseek" else OpenAICompatProvider
        kwargs = {"api_key": key, "model": selected.get("model_default") or provider_class.default_model,
                  "base_url": selected.get("base_url") or provider_class.default_base_url,
                  "proxy_mode": selected.get("proxy_mode") or "system",
                  "context_window": selected.get("context_window"),
                  "max_output_tokens": selected.get("max_output_tokens")}
        delegate = create_provider(name, **kwargs)
        router = ModelRouter(provider_name=name)
        return router, delegate, {"profile_id": profile_id, "provider": name, "model": delegate.model}
    router = ModelRouter() if live else ModelRouter(provider_name="offline")
    delegate = router.resolve_default() if live else offline_provider(workspace, scenario=scenario)
    if live and router.provider_name == "offline":
        raise ValueError("Live mode needs configured default provider")
    return router, delegate, {"profile_id": None, "provider": router.provider_name,
                              "model": getattr(delegate, "model", "offline-script")}


async def run_probe(root, *, live=False, timeout=900, max_turns=80, profile_id=None, scenario="combined"):
    if profile_id and not live:
        raise ValueError("--profile-id requires --live")
    probe_started = time.monotonic()
    targets = scenario_targets(scenario)
    workspace, initial = make_fixture(root, scenario=scenario)
    baseline_existing = external_verify(workspace, include_new=False, original_only=True, label="baseline-existing", scenario=scenario)
    baseline = external_verify(workspace, include_new=False, label="baseline-acceptance", scenario=scenario)
    if baseline_existing["exit_code"] or baseline["exit_code"] == 0 or baseline["errors"]:
        raise RuntimeError("Baseline must reproduce feature/bug failures while real existing tests pass")
    router, delegate, provider_meta = resolve_probe_provider(workspace, live=live, profile_id=profile_id, scenario=scenario)
    if live:
        delegate.max_output_tokens = min(getattr(delegate, "max_output_tokens", None) or 8192, 8192)
        if hasattr(delegate, "max_retries"):
            delegate.max_retries = 1
    provider = ProjectRecordingProvider(delegate, max_turns=max_turns)
    router.provider_override = provider
    previous_home = os.environ.get("CODE_AGENT_HOME")
    os.environ["CODE_AGENT_HOME"] = str(root / "agent-home")
    transcript = SafeTranscript(root / "transcripts", [getattr(delegate, "api_key", ""), getattr(delegate, "base_url", "")])
    (root / "requests").mkdir()
    provider.on_request = lambda turn, messages, tools: (root / "requests" / f"turn-{turn:03}.json").write_text(
        json.dumps(transcript.redact({"turn": turn, "messages": messages, "tools": tools}), ensure_ascii=False, indent=2), encoding="utf8")
    observer = ProjectObserver(workspace, provider, initial, scenario=scenario, minimum_tests=baseline["tests"] + 2)
    database = runtime = intel = result = submit_started = None
    started = time.monotonic()
    summary = {"mode": "live" if live else "offline", "scenario": scenario, "artifact_dir": str(root), **provider_meta,
               "max_turns": max_turns, "timeout_seconds": timeout, "copied_source_files": len(initial),
               "minimum_complete_suite_tests": baseline["tests"] + 2,
               "acceptance_source_sha256": hashlib.sha256(acceptance_source(scenario).encode()).hexdigest(),
               "baseline_existing": baseline_existing, "baseline_acceptance": baseline}
    immutable_before = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (root / "acceptance/test_search_delivery.py", root / "pytest.ini", root / "source-manifest.json")}
    try:
        from code_agent.api.app import build_registry
        from code_agent.codeintel.service import CodeIntelService
        from code_agent.sessions.manager import SessionRuntime
        from code_agent.storage.db import ApprovalRepo, Database, ProjectRepo, SessionRepo, UserRepo

        database = Database(root / "agent-home" / "project-validation.db")
        user = UserRepo(database).create("development-validation", "private-local-fixture")
        projects = ProjectRepo(database)
        project = projects.create(user["id"], "Actual IDE project search", str(workspace), str(workspace).lower())
        intel = CodeIntelService(projects)
        await intel.scan(project, full=True)
        registry = build_registry(code_intel=intel)
        configure_probe_tools(registry, workspace, scenario=scenario, shell_executions=observer.shell_executions,
                              edit_executions=observer.edit_executions)
        sessions = SessionRepo(database)
        meta = sessions.create(user["id"], title="Feature and bug fix delivery", project_id=project["id"],
                               tree_id="main", kind="chat", permission_mode="workspace-write")
        policies = {"bash": ProjectShellPolicy(scenario=scenario), "bash_persistent": ProjectShellPolicy(scenario=scenario),
                    "write_file": ProjectWritePolicy(workspace, scenario=scenario), "edit_file": ProjectWritePolicy(workspace, scenario=scenario),
                    "edit_many": ProjectWritePolicy(workspace, batch=True, scenario=scenario)}
        runtime = SessionRuntime(meta["id"], user["id"], transcript, sessions, ApprovalRepo(database), router, registry, workspace,
                                 extra_policies=policies, permission_mode="workspace-write", memory_root=root / "memory",
                                 session_memory_root=root / "session-memory")
        runtime.code_context_provider = lambda: intel.session_context(project)
        runtime._code_context_provider_fn = lambda selected: intel.message_context(project, selected)
        runtime.code_project, runtime.code_intel = project, intel

        async def approve(request):
            from code_agent.tools.base import ToolContext
            policy = policies.get(request.tool_name)
            return bool(policy and policy.evaluate(json.loads(request.command), ToolContext(meta["id"], workspace)).decision != Decision.deny)

        runtime._server_approver = approve
        original_broadcast = runtime.broadcast
        runtime.broadcast = lambda event: (observer.on_event(event), original_broadcast(event))[-1]
        original_build = runtime._build_loop

        def bounded_build(**overrides):
            remaining = max(1, max_turns - provider.calls)
            overrides["max_turns"] = min(remaining, int(overrides.get("max_turns") or remaining))
            return original_build(**overrides)

        runtime._build_loop = bounded_build
        summary["production_registry_tools"] = registry.names()
        submit_started = time.monotonic()
        result = await asyncio.wait_for(runtime.submit(task_prompt(scenario), code_context={"path": SOURCE}), timeout=timeout)
        summary.update(stop_reason=result.stop_reason, turns=provider.calls, usage=result.usage.model_dump(), answer_chars=len(result.text or ""))
    except asyncio.TimeoutError:
        summary.update(stop_reason="verification_timeout", turns=provider.calls)
    except Exception as exc:
        summary.update(stop_reason="verification_error", exception_type=type(exc).__name__, turns=provider.calls)
    finally:
        if submit_started is not None:
            summary["agent_submit_seconds"] = round(time.monotonic() - submit_started, 3)
        if runtime is not None:
            runtime.shutdown()
        if intel is not None:
            intel.close()
        if database is not None:
            database.close()
        await router.aclose()
        if previous_home is None:
            os.environ.pop("CODE_AGENT_HOME", None)
        else:
            os.environ["CODE_AGENT_HOME"] = previous_home
    final = external_verify(workspace, label="final", scenario=scenario)
    summary.update(observer.summary(), external_verification=final, elapsed_seconds=round(time.monotonic() - started, 2),
                   session_id=getattr(runtime, "session_id", None), request_summaries=provider.request_summaries)
    summary["probe_total_seconds"] = round(time.monotonic() - probe_started, 3)
    summary["independent_pytest_execution_seconds"] = round(sum(check["execution_seconds"] for check in (baseline_existing, baseline, final)), 3)
    summary["model_request_seconds"] = round(sum(request.get("elapsed_seconds", 0) for request in provider.request_summaries), 3)
    summary["model_token_usage"] = {key: sum(request.get("usage", {}).get(key, 0) for request in provider.request_summaries)
                                    for key in ("input_tokens", "cached_tokens", "output_tokens")}
    immutable = all((root / relative).is_file() and hashlib.sha256((root / relative).read_bytes()).hexdigest() == version
                    for relative, version in immutable_before.items())
    current = snapshot(workspace)
    original_unchanged = all(current.get(p) == version for p, version in initial.items() if p not in targets)
    new_test = (workspace / scenario_test(scenario)).read_text(encoding="utf8") if (workspace / scenario_test(scenario)).exists() else ""
    # External tests supply truth; this also requires actual new regression
    # coverage instead of a comments-only or empty-test delivery.
    tests_authored = len(re.findall(r"^\s*(?:async\s+)?def\s+test_", new_test, re.M)) >= 2 and "search_text" in new_test
    summary.update(independent_verifier_unchanged=immutable, original_dependencies_and_tests_unchanged=original_unchanged,
                   new_regression_tests_authored=tests_authored)
    summary["passed"] = bool(result and result.stop_reason == "end_turn" and final["exit_code"] == 0
                             and final["tests"] >= baseline["tests"] + 2 and not final["skipped"] and set(summary["changed_paths"]) == set(targets)
                             and set(summary["model_verified_paths"]) == set(targets) and not summary["out_of_scope_changes"]
                             and immutable and original_unchanged and tests_authored)
    for filename, data in (("tool-steps.json", transcript.redact(observer.steps)), ("summary.json", summary)):
        (root / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--profile-id", help="Read one existing profile for this live run without activating it")
    parser.add_argument("--scenario", choices=SCENARIOS, default="combined", help="Preserve combined benchmark or isolate feature/bug delivery")
    parser.add_argument("--output-dir", type=Path, default=REPO / "web/shots/project-development-delivery")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--max-turns", type=int, default=80)
    args = parser.parse_args()
    if args.profile_id and not args.live:
        parser.error("--profile-id requires --live")
    if not 0 < args.timeout <= 900 or not 1 <= args.max_turns <= 80:
        parser.error("timeout must be <=900 seconds and max-turns <=80")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=args.output_dir)).resolve()
    try:
        summary = asyncio.run(run_probe(root, live=args.live, timeout=args.timeout, max_turns=args.max_turns, profile_id=args.profile_id, scenario=args.scenario))
    except Exception as exc:
        summary = {"passed": False, "artifact_dir": str(root), "exception_type": type(exc).__name__}
        (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    printable = {key: value for key, value in summary.items() if key not in {"request_summaries", "production_registry_tools"}}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
