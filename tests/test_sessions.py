import threading
import time

from avd_feed_connect.gui.session import AccountSession
from avd_feed_connect.accounts import ProfileRegistry
from avd_feed_connect.auth.tokens import TokenStore


def session(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(TokenStore, '_keyring_clear', lambda *a: None)
    registry = ProfileRegistry(str(tmp_path))
    p = registry.upsert({'tid': 't', 'oid': 'o', 'upn': 'user@example.com'})
    return AccountSession(p, registry.client(), str(tmp_path / 'account'))


def test_late_refresh_cannot_restore_removed_credentials(tmp_path, monkeypatch):
    owner = session(tmp_path, monkeypatch)
    owner.client.save_refresh_token('old')
    started, finish = threading.Event(), threading.Event()
    def refresh(rt):
        started.set()
        assert finish.wait(3)
        return {'access_token': 'access', 'refresh_token': 'rotated', 'expires_in': 3600}
    monkeypatch.setattr(owner.client.oauth, '_refresh', refresh)
    worker = threading.Thread(target=owner.refresh)
    worker.start()
    assert started.wait(3)
    owner.close()
    finish.set()
    worker.join(3)
    assert not worker.is_alive()
    assert owner.client.load_token_record() is None
    assert owner.token is None


def test_feed_write_after_removal_is_discarded(tmp_path, monkeypatch):
    owner = session(tmp_path, monkeypatch)
    owner.close()
    assert not owner.save_resources([{'id': 'late'}])
    assert owner.resources == []


def test_refresh_keeps_account_identity(tmp_path, monkeypatch):
    import base64
    import json
    owner = session(tmp_path, monkeypatch)
    claims = base64.urlsafe_b64encode(json.dumps({'tid': 'other', 'oid': 'other'}).encode()).decode()
    assert not owner.apply_token({'access_token': 'wrong', 'id_token': 'x.' + claims + '.x'})
    assert owner.token is None


def test_network_failure_is_reported_without_losing_saved_account(tmp_path, monkeypatch):
    owner = session(tmp_path, monkeypatch)
    owner.client.save_refresh_token('keep')
    def offline(rt):
        raise OSError('Network unavailable')
    monkeypatch.setattr(owner.client.oauth, '_refresh', offline)
    assert owner.refresh() is False
    assert 'Network unavailable' in owner.client.last_refresh_error
    assert owner.client.load_token_record()['refresh_token'] == 'keep'


def test_display_name_prefers_alias_then_email_domain_not_workspace(tmp_path, monkeypatch):
    owner = session(tmp_path, monkeypatch)
    assert owner.display_name == 'example.com'
    owner.resources = [{'tenant_id': 't', 'tenant': 'Tessitura Network'},
                       {'tenant_id': 'other', 'tenant': 'Other company'}]
    assert owner.display_name == 'example.com'
    owner.profile['alias'] = 'My work'
    assert owner.display_name == 'My work'
    owner.profile.pop('alias')
    owner.resources = [{'tenant': 'First'}, {'tenant': 'Second'}]
    assert owner.display_name == 'example.com'
