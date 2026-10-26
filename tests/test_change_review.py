from pathlib import Path
from types import SimpleNamespace
import asyncio
import pytest
from code_agent.codeintel.change_review import ChangeReviewService, ReviewConflict
from code_agent.tools.builtin.propose_edit import ProposeEditTool
from code_agent.tools.base import ToolContext, ToolError


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    project = {'id': 'p', 'root_path': str(root), 'user_id': 'owner'}
    records = []
    intel = SimpleNamespace(record_patch=lambda **kw: records.append(kw) or {'id': str(len(records))}, project_for_workspace=lambda ws, uid: project if Path(ws) == root and uid == 'owner' else None)
    reviews = ChangeReviewService(intel, tmp_path / 'reviews')
    intel.reviews = reviews
    yield root, project, reviews, records, intel
    reviews.close()


def test_disjoint_hunks_apply_reverse_order_reject_and_rollback(fixture):
    root, project, reviews, records, _ = fixture
    before = ''.join(f'line {i}\n' for i in range(30))
    after = before.replace('line 1\n', 'first\nnew\n').replace('line 14\n', 'middle\n').replace('line 28\n', 'last\n')
    path = root / 'a.txt'
    path.write_text(before)
    proposal = reviews.create(project, {'path': 'a.txt', 'content': after})
    assert path.read_text() == before and len(proposal['hunks']) == 3
    reviews.decide(project, proposal['id'], 'apply', ['h3'])
    reviews.decide(project, proposal['id'], 'reject', ['h2'])
    result = reviews.decide(project, proposal['id'], 'apply', ['h1'])
    assert result['proposal']['state'] == 'reviewed'
    assert path.read_text() == before.replace('line 1\n', 'first\nnew\n').replace('line 28\n', 'last\n')
    reviews.decide(project, proposal['id'], 'rollback')
    assert path.read_text() == before and len(records) == 3


def test_disk_conflict_never_overwrites_intervening_change(fixture):
    root, project, reviews, _, _ = fixture
    target = root / 'a.py'
    target.write_text('x = 1\n')
    p = reviews.create(project, {'path': 'a.py', 'edits': [{'old_string': '1', 'new_string': '2'}]})
    target.write_text('x = 3\n')
    with pytest.raises(ReviewConflict): reviews.decide(project, p['id'], 'apply')
    assert target.read_text() == 'x = 3\n'
    target.write_text('x = 1\n')
    reviews.decide(project, p['id'], 'apply')
    target.write_text('x = 4\n')
    with pytest.raises(ReviewConflict): reviews.decide(project, p['id'], 'rollback')
    assert target.read_text() == 'x = 4\n'


def test_new_test_file_staged_apply_rollback_and_reopen(fixture):
    root, project, reviews, _, intel = fixture
    p = reviews.create(project, {'path': 'tests/test_new.py', 'content': 'def test_value():\n    assert 1 == 1\n'})
    target = root / p['path']
    assert p['new_file'] and not target.exists()
    reviews.decide(project, p['id'], 'apply')
    reviews.close()
    reopened = ChangeReviewService(intel, reviews.directory)
    try:
        assert reopened.detail(project, p['id'])['state'] == 'applied'
        reopened.decide(project, p['id'], 'rollback')
        assert not target.exists()
    finally: reopened.close()


def test_new_file_creation_race_and_workspace_escape_rejected(fixture):
    root, project, reviews, _, _ = fixture
    with pytest.raises(ValueError): reviews.create(project, {'path': '../outside.py', 'content': 'bad'})
    p = reviews.create(project, {'path': 'new.py', 'content': 'proposed'})
    target = root / 'new.py'
    target.write_text('created elsewhere')
    with pytest.raises(ReviewConflict): reviews.decide(project, p['id'], 'apply')
    assert target.read_text() == 'created elsewhere'


def test_patch_log_failure_keeps_proposal_rollback_available(fixture):
    root, project, reviews, _, intel = fixture
    (root / 'a.txt').write_text('before')
    p = reviews.create(project, {'path': 'a.txt', 'content': 'after'})
    def broken(**kwargs): raise OSError('log unavailable')
    intel.record_patch = broken
    result = reviews.decide(project, p['id'], 'apply')
    assert result['warning'] and result['proposal']['state'] == 'applied'
    reviews.decide(project, p['id'], 'rollback')
    assert (root / 'a.txt').read_text() == 'before'


def test_ai_proposal_does_not_write_and_is_owner_scoped(fixture):
    root, project, reviews, _, intel = fixture
    (root / 'a.py').write_text('x = 1\n')
    tool = ProposeEditTool(intel)
    ctx = ToolContext(session_id='s', workspace=root, user_id='owner')
    result = asyncio.run(tool.execute({'path': 'a.py', 'edits': [{'old_string': '1', 'new_string': '2'}]}, ctx))
    assert 'proposal_id' in result and (root / 'a.py').read_text() == 'x = 1\n'
    with pytest.raises(ToolError):
        asyncio.run(tool.execute({'path': 'a.py', 'edits': [{'old_string': '1', 'new_string': '2'}]}, ToolContext(session_id='s', workspace=root, user_id='another')))
