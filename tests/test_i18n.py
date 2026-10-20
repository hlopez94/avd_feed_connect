"""Catalog validation and deterministic language choice without global setlocale."""
import ast
import json
from pathlib import Path
from string import Formatter

import pytest

from avd_feed_connect import i18n
from avd_feed_connect.gui import storage


@pytest.fixture(autouse=True)
def reset_language():
    yield
    i18n.configure('en')


@pytest.mark.parametrize('locale,expected', [
    ('en_GB.UTF-8', 'en'), ('es_AR.UTF-8', 'es-AR'), ('es_ES@euro', 'es-ES'),
    ('es_MX.UTF-8', 'es-419'), ('es_CL', 'es-419'), ('es-419', 'es-419'),
    ('es', 'es-419'), ('fr_FR', 'en'), ('es_GQ', 'en'), ('C.UTF-8', 'en')])
def test_locale_mapping(locale, expected):
    assert i18n.system_language({'LANG': locale}) == expected


@pytest.mark.parametrize('env,expected', [
    ({'LANG': 'es_AR', 'LC_MESSAGES': 'es_ES'}, 'es-ES'),
    ({'LANG': 'es_AR', 'LC_MESSAGES': 'es_ES', 'LC_ALL': 'fr_FR'}, 'en'),
    ({'LANG': 'en_US.UTF-8', 'LANGUAGE': 'es_AR:es:en'}, 'es-AR'),
    ({'LANG': 'es_AR', 'LANGUAGE': 'fr_FR:es'}, 'en'),
    ({'LC_ALL': 'C.UTF-8', 'LANGUAGE': 'es_AR'}, 'en'),
    ({'LANG': 'es_AR', 'LC_ALL': '', 'LC_MESSAGES': ''}, 'es-AR')])
def test_environment_precedence(env, expected):
    assert i18n.system_language(env) == expected


def test_selection_persistence_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    for language, _label in i18n.LANGUAGES:
        storage.save_settings({'_language': language, '_theme': 'dark'})
        saved = storage.load_settings()
        assert saved == {'_language': language, '_theme': 'dark'}
        selected = i18n.configure(saved['_language'], {'LANG': 'es_AR'})
        assert selected == ('es-AR' if language == 'system' else language)
    i18n.configure('es-AR')
    assert i18n._('Save') == 'Guardar'
    assert i18n._('Missing string {value}') == 'Missing string {value}'
    assert i18n._('Please use the selected work account.').startswith('Usá')
    i18n.configure('es-ES')
    assert i18n._('Add account') == 'Añadir cuenta'
    i18n.configure('unsupported')
    assert i18n._('Save') == 'Save'


def test_missing_and_empty_catalog_entries_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, 'CATALOG_DIR', tmp_path)
    (tmp_path / 'es.json').write_text('{"Save": ""}')
    i18n.configure('es-AR')
    assert i18n._('Save') == 'Save'
    assert i18n._('Cancel') == 'Cancel'


@pytest.mark.parametrize('language', ['en', 'es-419', 'es-ES', 'es-AR'])
@pytest.mark.parametrize('count', [0, 1, 2, 100])
def test_plurals(language, count):
    i18n.configure(language)
    result = i18n.ngettext('{count} resource', '{count} resources', count).format(count=count)
    noun = 'resource' if language == 'en' else 'recurso'
    assert result == f'{count} {noun}' + ('' if count == 1 else 's')


def test_catalogs_cover_source_and_preserve_placeholders():
    catalogs = {p.stem: json.loads(p.read_text()) for p in i18n.CATALOG_DIR.glob('*.json')}
    base = catalogs['es']
    fields = lambda text: {field for _, field, _, _ in Formatter().parse(text) if field}
    for name, catalog in catalogs.items():
        for source, translation in catalog.items():
            assert translation and fields(source) == fields(translation), (name, source)
            assert source in base
    for path in Path('src').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('tr', '_', 'ngettext'):
                for arg in node.args[:2 if node.func.id == 'ngettext' else 1]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert arg.value in base, (path, arg.value)
    for labels in (storage.SCALE_LABELS, storage.MULTIMON_LABELS, storage.CERT_LABELS,
                   storage.HOTKEY_LABELS, storage.UA_LABELS):
        assert all(label in base or label.endswith('%') for label in labels)


def test_translated_tray_preserves_remote_names():
    from avd_feed_connect.gui.tray_menu import build_menu
    i18n.configure('es-AR')
    menu = build_menu([{'id': 'opaque-id', 'title': 'Finance PC', 'tenant': 'Northstar'}], {})
    assert menu[0]['label'] == 'NORTHSTAR'
    assert menu[1]['label'] == 'Finance PC'
    assert menu[1]['children'][0]['label'] == 'Conectar'
    assert menu[1]['children'][0]['action'] == ('connect', 'opaque-id')
    assert menu[-1]['label'] == 'Salir'


@pytest.mark.parametrize('value', [None, 123, {}, ''])
def test_invalid_preferences_use_english(value):
    assert i18n.configure(value) == 'en'


@pytest.mark.parametrize('translation', ['{wrong}', '{resource', '{resource:d}', None, 123, ''])
def test_malformed_translations_fall_back_without_losing_arguments(tmp_path, monkeypatch, translation):
    monkeypatch.setattr(i18n, 'CATALOG_DIR', tmp_path)
    (tmp_path / 'es.json').write_text(json.dumps({'Connected to {resource}': translation}))
    i18n.configure('es-AR')
    assert str(i18n._('Connected to {resource}').format(resource='Original name')) == 'Connected to Original name'


def test_message_retains_source_and_nested_arguments_across_switches():
    message = i18n._('{automatic} Applies to your next connection.').format(
        automatic=i18n._('Automatic matches your display and monitors.'))
    i18n.configure('es-AR')
    assert message.render().startswith('Automático se adapta')
    i18n.configure('unsupported')
    assert message.render() == 'Automatic matches your display and monitors. Applies to your next connection.'
