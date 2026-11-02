"""The IDE integration routes require an explicit action and project ownership."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from code_agent.api.deps import CurrentUser, get_current_user
from code_agent.api.integration_api import mount_integration_api


class Integration:
    def __init__(self):
        self.actions = []

    def check(self, pid, uid):
        if pid != 'p' or uid != 'owner':
            raise KeyError('项目不存在')

    def configuration(self, pid, uid):
        self.check(pid, uid)
        return {'config': {'name': 'demo'}, 'candidates': {'frontend': [], 'backend': []}}

    def snapshot(self, pid, uid):
        self.check(pid, uid)
        return {'session': None}

    def save_config(self, pid, uid, config):
        self.check(pid, uid)
        self.actions.append(('save', config))
        return {'config': config, 'candidates': {'frontend': [], 'backend': []}}

    def start(self, pid, uid, config=None):
        self.check(pid, uid)
        self.actions.append(('start', config))
        return {'session': {'id': 's', 'status': 'starting'}}

    def stop(self, pid, uid, role='all'):
        self.check(pid, uid)
        self.actions.append(('stop', role))
        return {'session': {'id': 's', 'status': 'stopping'}}

    def restart(self, pid, uid, role='all'):
        self.check(pid, uid)
        self.actions.append(('restart', role))
        return {'session': {'id': 's2', 'status': 'starting'}}


@pytest.fixture
def client():
    app = FastAPI()
    app.state.auth_enabled = False
    app.state.local_user = CurrentUser('owner', 'owner', 'user')
    service = Integration()
    mount_integration_api(app, runtime=None, project_service=None, integration=service,
                          proxy=SimpleNamespace(blocked_ports=set()))
    with TestClient(app) as http:
        yield http, service, app


BASE = '/api/v1/code/projects/p/integration'


def test_reading_state_or_configuration_does_not_run_or_save_anything(client):
    http, service, _ = client
    assert http.get(BASE).json() == {'session': None}
    assert http.get(BASE + '/config').json()['config']['name'] == 'demo'
    assert service.actions == []


def test_explicit_actions_and_selected_role_are_forwarded(client):
    http, service, _ = client
    saved = http.put(BASE + '/config', json={'frontend': {'kind': 'npm', 'script': 'dev'}, 'backend': {'kind': 'python', 'path': 'server.py'}})
    assert saved.status_code == 200
    assert service.actions[-1][0] == 'save'
    assert service.actions[-1][1]['frontend']['args'] == []
    assert http.post(BASE + '/start').status_code == 202
    assert service.actions[-1] == ('start', None)
    assert http.post(BASE + '/restart', json={'role': 'backend'}).status_code == 202
    assert service.actions[-1] == ('restart', 'backend')
    assert http.post(BASE + '/stop', json={'role': 'all'}).status_code == 202
    assert service.actions[-1] == ('stop', 'all')


def test_invalid_config_or_role_never_launches(client):
    http, service, _ = client
    assert http.post(BASE + '/start', json={'startup_timeout': 0}).status_code == 422
    assert http.put(BASE + '/config', json={'unexpected': 'value'}).status_code == 422
    assert http.post(BASE + '/stop', json={'role': 'unrelated'}).status_code == 422
    assert service.actions == []


def test_foreign_project_and_user_cannot_read_or_act(client):
    http, service, app = client
    assert http.get(BASE.replace('/p/', '/foreign/')).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: CurrentUser('other', 'other', 'user')
    assert http.get(BASE).status_code == 404
    assert http.post(BASE + '/start').status_code == 404
    assert service.actions == []


def test_authenticated_deployment_requires_its_normal_login(client):
    http, service, app = client
    app.state.auth_enabled = True
    assert http.get(BASE).status_code == 401
    assert http.post(BASE + '/start').status_code == 401
    assert service.actions == []
