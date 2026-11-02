"""Only genuine missing files are 404; temporary fixtures never alias names."""
from __future__ import annotations

from pathlib import Path

import pytest

from code_agent.codeintel.paths import ProjectPathError
from code_agent.codeintel.projects import ProjectFileNotFoundError
from test_editor_save import editor


SINGULAR = "sky-server/src/main/java/com/sky/SkyApplication.java"
PLURAL = "sky-server/src/main/java/com/sky/SkyApplications.java"


def request(editor, path, **kwargs):
    return editor.client.get("/api/v1/code/projects/p/file", params={"path": path}, **kwargs)


def make_source(editor):
    target = editor.root / SINGULAR
    target.parent.mkdir(parents=True)
    target.write_text("public class SkyApplication {}\n", encoding="utf-8")
    return target


def test_missing_type_keeps_path_error_compatibility():
    error = ProjectFileNotFoundError("文件不存在：missing.java")
    assert isinstance(error, ProjectPathError)
    assert error.code == "PROJECT_FILE_NOT_FOUND"


def test_missing_file_has_explicit_404_code_and_existing_string_detail(editor):
    make_source(editor)
    response = request(editor, PLURAL)
    assert response.status_code == 404
    assert response.headers["X-Code-Agent-Error-Code"] == "PROJECT_FILE_NOT_FOUND"
    assert response.json() == {"detail": "文件不存在：" + PLURAL}
    assert not (editor.root / PLURAL).exists()


def test_real_name_is_preserved_and_plural_never_aliases_singular(editor):
    target = make_source(editor)
    original = target.read_bytes()
    result = request(editor, SINGULAR)
    assert result.status_code == 200
    body = result.json()
    assert body["path"] == SINGULAR and body["uri"].endswith("/SkyApplication.java")
    assert body["editable"] is True and body["text"] == "public class SkyApplication {}\n"
    assert request(editor, PLURAL).status_code == 404
    assert target.read_bytes() == original and not (editor.root / PLURAL).exists()


def test_reads_after_explicit_fixture_rename_use_current_paths_without_old_name_cache(editor):
    target = make_source(editor)
    assert request(editor, SINGULAR).status_code == 200
    assert request(editor, PLURAL).status_code == 404
    destination = editor.root / PLURAL
    target.rename(destination)  # Only this test's isolated temporary project.
    assert request(editor, SINGULAR).status_code == 404
    renamed = request(editor, PLURAL)
    assert renamed.status_code == 200 and renamed.json()["path"] == PLURAL
    assert renamed.json()["uri"].endswith("/SkyApplications.java")
    destination.rename(target)
    assert request(editor, PLURAL).status_code == 404
    assert request(editor, SINGULAR).status_code == 200


@pytest.mark.parametrize("path", ["../outside.java", "folder", "binary.bin"])
def test_invalid_directory_and_binary_errors_stay_422_not_missing(editor, path):
    (editor.root / "folder").mkdir()
    (editor.root / "binary.bin").write_bytes(b"\0binary")
    response = request(editor, path)
    assert response.status_code == 422
    assert "X-Code-Agent-Error-Code" not in response.headers


def test_read_permission_error_is_not_reclassified_as_missing(editor, monkeypatch):
    original_open = Path.open
    def denied(path, *args, **kwargs):
        if path == editor.target:
            raise PermissionError("fixture read denied")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    response = request(editor, "a.txt")
    # Preserve the existing error mapping; this change only classifies absent
    # files and must never manufacture a missing-file result for access errors.
    assert response.status_code == 500
    assert "PermissionError" in response.json()["detail"]
    assert "X-Code-Agent-Error-Code" not in response.headers


def test_project_ownership_404_does_not_claim_the_file_is_missing(editor):
    response = request(editor, "a.txt", headers={"X-Fixture-User": "another"})
    assert response.status_code == 404
    assert "X-Code-Agent-Error-Code" not in response.headers


def test_save_of_missing_file_does_not_create_or_rewrite_the_real_name(editor):
    target = make_source(editor)
    before = target.read_bytes()
    response = editor.client.put("/api/v1/code/projects/p/file", json={
        "path": PLURAL, "content": "must not create\n", "expected_sha1": "", "session_id": "fixture-editor"})
    assert response.status_code == 404
    assert not (editor.root / PLURAL).exists()
    assert target.read_bytes() == before
