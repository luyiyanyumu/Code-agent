"""Configuration and release invariants for standalone distribution."""
from pathlib import Path
import subprocess
import sys
import logging

from chat_agent.config import Config
from chat_agent.server import RedactQueryTokens
from chat_agent.api import create_app
from fastapi.testclient import TestClient


def test_blank_home_template_stays_in_user_data_directory(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_HOME", "")
    monkeypatch.setenv("CHAT_AGENT_PROVIDER", "")
    config = Config.from_env()
    assert config.home == (Path.home() / ".chat-agent").resolve()
    assert config.provider == "offline"


def test_cli_rejects_invalid_port_before_starting(tmp_path):
    result = subprocess.run([sys.executable, "-m", "chat_agent.server", "--home", str(tmp_path), "--port", "0"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "Port must be between 1 and 65535" in result.stderr


def test_license_and_frontend_notices_present():
    root = Path(__file__).resolve().parents[1]
    assert "Apache License" in (root / "LICENSE").read_text(encoding="utf-8")
    assert "Version 2.0" in (root / "LICENSE").read_text(encoding="utf-8")
    for name in ("vue.txt", "marked.txt"):
        assert "permission" in (root / "licenses" / name).read_text(encoding="utf-8").lower()


def test_websocket_query_credentials_are_redacted_from_server_logs():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, 'WebSocket %s accepted',
                               ("/stream?token=test-private-token&after_seq=7",), None)
    RedactQueryTokens().filter(record)
    assert "test-private-token" not in record.getMessage()
    assert "token=[redacted]&after_seq=7" in record.getMessage()


def test_session_creation_respects_configured_permission_default(tmp_path):
    with TestClient(create_app(Config(home=tmp_path, default_permission="read-only")), base_url="http://127.0.0.1") as client:
        assert client.post("/api/v1/sessions", json={}).json()["permission_mode"] == "read-only"
        assert client.post("/api/v1/sessions", json={"permission_mode": "workspace-write"}).json()["permission_mode"] == "workspace-write"
