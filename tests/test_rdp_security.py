"""Security mode selection without a network connection or real credentials."""
import pytest
from avd_feed_connect.rdp.security import resolve_security


@pytest.mark.parametrize('text,mode', [
    ('enablerdsaadauth:i:1', 'aad'),
    (' EnableRdsAadAuth : i : 1\r\n', 'aad'),
    ('enablerdsaadauth:i:0\naadtenantid:s:example', 'nla'),
    ('aadtenantid:s:example', 'nla'),
    ('targetisaadjoined:i:1', 'nla'),
    ('', 'nla'),
    ('signscope:s:enablerdsaadauth:i:1', 'nla'),
    ('enablerdsaadauth:i:10', 'nla'),
    ('enablerdsaadauth:i:1\nenablerdsaadauth:i:0', 'nla'),
])
def test_auto_respects_explicit_properties(tmp_path, monkeypatch, text, mode):
    monkeypatch.delenv('AVD_SEC', raising=False)
    path = tmp_path / 'sample.rdp'
    path.write_text(text)
    assert resolve_security(path).mode == mode


@pytest.mark.parametrize('override', ['aad', 'nla'])
def test_explicit_setting_overrides_feed(tmp_path, monkeypatch, override):
    monkeypatch.delenv('AVD_SEC', raising=False)
    p = tmp_path / 'a.rdp'; p.write_text('enablerdsaadauth:i:0')
    decision = resolve_security(p, override)
    assert decision.mode == override
    assert decision.reason == 'connection setting'


def test_environment_has_priority_and_consumes_advanced_security_flags(tmp_path, monkeypatch):
    monkeypatch.setenv('AVD_SEC', 'AAD')
    p = tmp_path / 'a.rdp'; p.write_text('enablerdsaadauth:i:0')
    d = resolve_security(p, 'nla', '/sec:nla /gfx /d:COMPANY')
    assert d.mode == 'aad'
    assert d.options == ['/gfx', '/d:COMPANY']
    assert d.reason == 'AVD_SEC override'


def test_advanced_security_override_without_setting(tmp_path, monkeypatch):
    monkeypatch.delenv('AVD_SEC', raising=False)
    p = tmp_path / 'a.rdp'; p.write_text('aadtenantid:s:example')
    assert resolve_security(p, extra='/sec:aad').mode == 'aad'


@pytest.mark.parametrize('value', ['tls', 'invalid'])
def test_unknown_environment_override_is_an_error(tmp_path, monkeypatch, value):
    monkeypatch.setenv('AVD_SEC', value)
    (tmp_path / 'a.rdp').write_text('')
    with pytest.raises(ValueError, match='AVD_SEC'):
        resolve_security(tmp_path / 'a.rdp')


def test_missing_file_is_not_silently_classified(tmp_path, monkeypatch):
    monkeypatch.delenv('AVD_SEC', raising=False)
    with pytest.raises(OSError):
        resolve_security(tmp_path / 'missing.rdp')


def test_remembered_success_applies_only_to_unchanged_target(tmp_path, monkeypatch):
    monkeypatch.delenv('AVD_SEC', raising=False)
    p = tmp_path / 'a.rdp';p.write_text('aadtenantid:s:example\nfull address:s:desktop')
    original = resolve_security(p)
    remembered = {'mode': 'aad', 'fingerprint': original.fingerprint}
    assert resolve_security(p, remembered=remembered).mode == 'aad'
    p.write_text('aadtenantid:s:changed\nfull address:s:desktop')
    assert resolve_security(p, remembered=remembered).mode == 'nla'


def test_explicit_feed_disable_has_priority_over_remembered_mode(tmp_path, monkeypatch):
    monkeypatch.delenv('AVD_SEC', raising=False)
    p = tmp_path / 'a.rdp';p.write_text('enablerdsaadauth:i:0')
    original = resolve_security(p)
    assert resolve_security(p, remembered={'mode':'aad','fingerprint':original.fingerprint}).mode == 'nla'
