import base64
import json
import os

import pytest

from avd_feed_connect.accounts import ProfileRegistry, claims_from_token, profile_dir
from avd_feed_connect.auth.tokens import TokenStore


def token(tid='tenant-a', oid='user-a', upn='same@example.com'):
    claims = {'tid': tid, 'oid': oid, 'preferred_username': upn}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return {'id_token': 'header.' + payload + '.signature'}


def test_identity_and_selection_survive_restart(tmp_path):
    registry = ProfileRegistry(str(tmp_path))
    a = registry.upsert(claims_from_token(token()))
    b = registry.upsert(claims_from_token(token(tid='tenant-b')))
    assert a['id'] != b['id']
    assert registry.upsert(claims_from_token(token()))['id'] == a['id']
    registry.select(b['id'])
    loaded = ProfileRegistry(str(tmp_path))
    assert loaded.selected_id == b['id']
    assert len(loaded.profiles) == 2
    loaded.remove(b['id'])
    assert loaded.selected_id == a['id']


@pytest.mark.parametrize('bad', ['../escape', '/tmp/escape', '', 'a/b'])
def test_profile_paths_reject_invalid_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        profile_dir(str(tmp_path), bad)


@pytest.mark.parametrize('response', [{}, {'id_token': 'bad'}, token(oid='')])
def test_missing_identity_is_rejected(response):
    with pytest.raises(ValueError):
        claims_from_token(response)


def test_account_secrets_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(TokenStore, '_keyring_clear', lambda *a: None)
    registry = ProfileRegistry(str(tmp_path))
    a = registry.upsert(claims_from_token(token()))
    b = registry.upsert(claims_from_token(token(tid='tenant-b')))
    ca, cb = registry.client(a['id']), registry.client(b['id'])
    ca.save_refresh_token('secret-a')
    cb.save_refresh_token('secret-b')
    ca.clear_token_cache()
    assert ca.load_token_record() is None
    assert cb.load_token_record()['refresh_token'] == 'secret-b'
    assert ca.output_dir != cb.output_dir
    assert os.stat(cb.tokens._cache).st_mode & 0o777 == 0o600


def test_migration_retries_after_registry_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(TokenStore, '_keyring_clear', lambda *a: None)
    legacy = TokenStore(str(tmp_path / 'token-cache.json'))
    legacy.save('legacy-secret')
    (tmp_path / 'workspaces.json').write_text(json.dumps({'upn': 'old@example.com', 'resources': [{'id': 'one'}]}))
    (tmp_path / 'settings.json').write_text(json.dumps({'_theme': 'dark', '_default': {'scale': '125'}, 'one': {'scale': '150'}}))
    registry = ProfileRegistry(str(tmp_path))
    original_clear = legacy.clear
    legacy.clear = lambda: (_ for _ in ()).throw(OSError('interrupted'))
    with pytest.raises(OSError):
        registry.migrate_legacy(legacy)
    assert registry.client().load_token_record()['refresh_token'] == 'legacy-secret'
    legacy.clear = original_clear
    registry = ProfileRegistry(str(tmp_path))
    registry.migrate_legacy(legacy)
    assert len(registry.profiles) == 1
    assert not legacy.has()
    assert json.loads((tmp_path / 'settings.json').read_text()) == {'_theme': 'dark', '_default': {'scale': '125'}}
    account = profile_dir(str(tmp_path), registry.selected_id)
    assert json.load(open(os.path.join(account, 'settings.json'))) == {'one': {'scale': '150'}}
    assert json.load(open(os.path.join(account, 'workspaces.json')))['upn'] == 'old@example.com'


def test_migration_retries_cleanup_after_legacy_secret_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(TokenStore, '_keyring_clear', lambda *a: None)
    legacy = TokenStore(str(tmp_path / 'token-cache.json'))
    legacy.save('secret')
    (tmp_path / 'workspaces.json').write_text('{"upn":"old@example.com","resources":[]}')
    registry = ProfileRegistry(str(tmp_path))
    import avd_feed_connect.accounts as accounts
    original = accounts.write_json
    def interrupted(path, data):
        if path == str(tmp_path / 'settings.json'):
            raise OSError('cleanup interrupted')
        original(path, data)
    monkeypatch.setattr(accounts, 'write_json', interrupted)
    with pytest.raises(OSError):
        registry.migrate_legacy(legacy)
    monkeypatch.setattr(accounts, 'write_json', original)
    ProfileRegistry(str(tmp_path)).migrate_legacy(legacy)
    assert not (tmp_path / 'workspaces.json').exists()
    assert registry.client().load_token_record()['refresh_token'] == 'secret'


def test_account_alias_survives_signin_and_can_be_reset(tmp_path):
    registry = ProfileRegistry(str(tmp_path))
    claims = claims_from_token(token())
    profile = registry.upsert(claims)
    registry.rename(profile['id'], '  My company  ')
    registry.upsert(claims)
    loaded = ProfileRegistry(str(tmp_path))
    assert loaded.profiles[0]['alias'] == 'My company'
    loaded.rename(profile['id'], '')
    assert 'alias' not in ProfileRegistry(str(tmp_path)).profiles[0]
