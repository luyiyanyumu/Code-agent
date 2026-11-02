import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from code_agent.api.code_api import mount_code_api
from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.api.native_dialog_api import mount_native_dialog_api, _pick_until_disconnect
from code_agent.codeintel import native_dialog


@pytest.fixture
def app(monkeypatch, tmp_path):
    app = FastAPI()
    app.state.auth_enabled = True
    app.dependency_overrides[get_current_user] = lambda: CurrentUser('u', 'user', 'user')
    monkeypatch.setattr(native_dialog, 'capabilities', lambda: {'available': True, 'platform': 'win32', 'local_only': True})
    monkeypatch.delenv('CODE_AGENT_CODE_BROWSE', raising=False)
    mount_native_dialog_api(app)
    mount_code_api(app, project_repo=None, manager=None, service=SimpleNamespace(roots=lambda: tmp_path))
    return app


def request(app, method, path='/api/v1/code/fs/pick-folder', *, client='127.0.0.1', body=None, headers=None):
    async def run():
        transport = httpx.ASGITransport(app=app, client=(client, 1234))
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as caller:
            return await caller.request(method, path, json=body, headers=headers)
    return asyncio.run(run())


def test_local_folder_selection_and_cancel_are_normal_responses(app, monkeypatch):
    calls = []
    async def choose(start):
        calls.append(start)
        return {'path': 'E:/中文项目', 'cancelled': False} if start else {'path': None, 'cancelled': True}
    monkeypatch.setattr(native_dialog, 'pick_folder', choose)
    selected = request(app, 'POST', body={'start': ' E:/中文项目 '})
    assert selected.status_code == 200 and selected.json()['path'] == 'E:/中文项目'
    cancelled = request(app, 'POST', body={})
    assert cancelled.status_code == 200 and cancelled.json()['cancelled']
    assert calls == ['E:/中文项目', '']


@pytest.mark.parametrize('host,forwarded', [('203.0.113.8', ''), ('127.0.0.1', '203.0.113.8'), ('127.0.0.1', 'invalid')])
def test_remote_requests_never_open_host_dialog(app, monkeypatch, host, forwarded):
    async def forbidden(start): raise AssertionError('native window must not open')
    monkeypatch.setattr(native_dialog, 'pick_folder', forbidden)
    headers = {'X-Forwarded-For': forwarded} if forwarded else {}
    response = request(app, 'POST', client=host, headers=headers, body={})
    assert response.status_code == 403
    meta = request(app, 'GET', '/api/v1/code/meta', client=host, headers=headers)
    assert not meta.json()['native_picker']['available']


def test_meta_reports_local_availability_without_opening_a_dialog(app, monkeypatch):
    async def forbidden(start): raise AssertionError('metadata must not open a dialog')
    monkeypatch.setattr(native_dialog, 'pick_folder', forbidden)
    meta = request(app, 'GET', '/api/v1/code/meta', client='::1')
    assert meta.status_code == 200 and meta.json()['native_picker']['available']
    monkeypatch.setenv('CODE_AGENT_CODE_BROWSE', '0')
    meta = request(app, 'GET', '/api/v1/code/meta')
    assert not meta.json()['native_picker']['available']
    assert request(app, 'POST', body={}).status_code == 503


def test_authentication_and_body_validation_happen_before_launch(app, monkeypatch):
    async def forbidden(start): raise AssertionError('invalid request must not open a dialog')
    monkeypatch.setattr(native_dialog, 'pick_folder', forbidden)
    assert request(app, 'POST', body={'start': '\x00'}).status_code == 422
    assert request(app, 'POST', body={'start': 1}).status_code == 422
    assert request(app, 'POST', body={'start': 'x' * 4097}).status_code == 422
    assert request(app, 'POST', body={'unexpected': True}).status_code == 422
    app.dependency_overrides.clear()
    assert request(app, 'POST', body={}).status_code == 401


@pytest.mark.parametrize('exception,status', [('NativeDialogBusy', 409), ('NativeDialogUnavailable', 503), ('NativeDialogError', 503)])
def test_dialog_failures_have_useful_http_errors(app, monkeypatch, exception, status):
    async def fail(start): raise getattr(native_dialog, exception)('系统选择器状态')
    monkeypatch.setattr(native_dialog, 'pick_folder', fail)
    response = request(app, 'POST', body={})
    assert response.status_code == status and response.json()['detail'] == '系统选择器状态'


def test_disconnected_request_cancels_only_its_own_picker_and_waits_for_cleanup(monkeypatch):
    async def check():
        disconnected = asyncio.Event()
        started = asyncio.Event()
        cleaned = []
        async def choose(start):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(start)
        async def is_disconnected():
            return disconnected.is_set()
        monkeypatch.setattr(native_dialog, 'pick_folder', choose)
        def forbidden_global_cancel():
            pytest.fail('an old request must never cancel the globally active selector')
        monkeypatch.setattr(native_dialog, 'cancel_active', forbidden_global_cancel)
        pending = asyncio.create_task(_pick_until_disconnect(SimpleNamespace(is_disconnected=is_disconnected), 'first'))
        await started.wait()
        disconnected.set()
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 499
        assert cleaned == ['first']
        async def next_choose(start):
            return {'path': start, 'cancelled': False}
        monkeypatch.setattr(native_dialog, 'pick_folder', next_choose)
        # Even a late disconnect from the old client must not touch the next picker.
        assert await _pick_until_disconnect(SimpleNamespace(is_disconnected=is_disconnected), 'second') == {'path': 'second', 'cancelled': False}
    asyncio.run(check())


def test_cancelled_route_task_cleans_up_its_picker(monkeypatch):
    async def check():
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def choose(start):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async def connected():
            return False
        monkeypatch.setattr(native_dialog, 'pick_folder', choose)
        pending = asyncio.create_task(_pick_until_disconnect(SimpleNamespace(is_disconnected=connected), 'first'))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cleaned.is_set()
    asyncio.run(check())
