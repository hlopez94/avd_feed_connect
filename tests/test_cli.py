import sys

from avd_feed_connect import cli
from avd_feed_connect.accounts import ProfileRegistry
from avd_feed_connect.auth.tokens import TokenStore


def test_cli_logout_clears_selected_keyring_and_preserves_other_account(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(TokenStore, '_keyring_clear', lambda *a: None)
    registry = ProfileRegistry(str(tmp_path))
    a = registry.upsert({'tid': 'a', 'oid': 'a', 'upn': 'a@example.com'})
    b = registry.upsert({'tid': 'b', 'oid': 'b', 'upn': 'b@example.com'})
    registry.client(a['id']).save_refresh_token('a')
    registry.client(b['id']).save_refresh_token('b')
    monkeypatch.setattr(cli, 'ProfileRegistry', lambda: ProfileRegistry(str(tmp_path)))
    monkeypatch.setattr(sys, 'argv', ['avd-feed', 'logout'])
    cli.main()
    loaded = ProfileRegistry(str(tmp_path))
    assert loaded.selected_id == a['id']
    assert loaded.client().load_token_record()['refresh_token'] == 'a'
    assert registry.client(b['id']).load_token_record() is None


def test_cli_rejects_different_identity_before_saving_credentials(tmp_path, monkeypatch):
    import base64
    import json
    import pytest
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    registry = ProfileRegistry(str(tmp_path))
    registry.upsert({'tid': 'company-a', 'oid': 'a', 'upn': 'a@example.com'})
    client = registry.client()
    payload = base64.urlsafe_b64encode(json.dumps({'tid': 'company-b', 'oid': 'b'}).encode()).decode()
    monkeypatch.setattr(client.oauth, '_auth_code', lambda: {'access_token': 'wrong', 'refresh_token': 'wrong-secret', 'expires_in': 3600, 'id_token': 'x.' + payload + '.x'})
    with pytest.raises(ValueError, match='selected work account'):
        client.get_token(verbose=False)
    assert client.load_token_record() is None


def test_cli_interactive_account_requires_identity(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(TokenStore, '_keyring_load', lambda *a: None)
    registry = ProfileRegistry(str(tmp_path))
    registry.upsert({'tid': 'a', 'oid': 'a', 'upn': 'a@example.com'})
    client = registry.client()
    monkeypatch.setattr(client.oauth, '_auth_code', lambda: {'access_token': 'unidentified', 'refresh_token': 'unknown', 'expires_in': 3600})
    with pytest.raises(ValueError):
        client.get_token(verbose=False)
    assert client.load_token_record() is None
