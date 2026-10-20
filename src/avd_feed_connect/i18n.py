"""Small UI catalog loader. English source strings are the final fallback.

The three Spanish variants deliberately share ``es.json``; regional catalogs
only contain terminology overrides. No process locale is changed.
"""
import json
import locale
import os
from collections import Counter
from string import Formatter
from pathlib import Path

LANGUAGES = (("system", "System default"), ("en", "English"),
             ("es-419", "Spanish (Latin America)"),
             ("es-ES", "Spanish (Spain)"), ("es-AR", "Spanish (Argentina)"))
CATALOG_DIR = Path(__file__).with_name("locales")
_catalog = {}
_listeners = []


def on_language_changed(callback):
    """Register a process-wide UI refresh hook (called on the GUI thread)."""
    _listeners.append(callback)


def resolve_language(value):
    if not isinstance(value, str):
        return "en"
    value = value.split(".", 1)[0].split("@", 1)[0].replace("_", "-").lower()
    if value == "en" or value.startswith("en-"):
        return "en"
    if value == "es-es":
        return "es-ES"
    if value == "es-ar":
        return "es-AR"
    if value == "es" or value == "es-419" or value in {
        "es-bo", "es-cl", "es-co", "es-cr", "es-cu", "es-do", "es-ec",
        "es-sv", "es-gt", "es-hn", "es-mx", "es-ni", "es-pa", "es-py",
        "es-pe", "es-pr", "es-uy", "es-ve", "es-us", "es-bz"}:
        return "es-419"
    return "en"


def system_language(environ=None):
    env = os.environ if environ is None else environ
    # GNU LANGUAGE overrides message locale, except the POSIX C locale.
    selected = next((env[k] for k in ("LC_ALL", "LC_MESSAGES", "LANG") if env.get(k)), "")
    if selected.split(".")[0] in ("C", "POSIX"):
        return "en"
    if env.get("LANGUAGE"):
        return resolve_language(env["LANGUAGE"].split(":")[0])
    if not selected:
        selected = locale.getlocale(locale.LC_MESSAGES)[0] or "en"
    return resolve_language(selected)


def configure(preference="system", environ=None):
    global _catalog
    language = system_language(environ) if preference == "system" else resolve_language(preference)
    _catalog = {}
    if language.startswith("es-"):
        for name in ("es", language):
            try:
                data = json.loads((CATALOG_DIR / (name + ".json")).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for source, translated in data.items():
                        if isinstance(source, str):
                            _catalog[source] = translated if _valid_translation(source, translated) else source
            except (OSError, ValueError):
                continue
    for callback in _listeners:
        callback()
    return language


def _valid_translation(source, translated):
    if not isinstance(translated, str) or not translated:
        return False
    try:
        fields = lambda text: Counter((field, spec, conversion)
                                      for _, field, spec, conversion in Formatter().parse(text)
                                      if field is not None)
        return fields(source) == fields(translated)
    except ValueError:
        return False


def _lookup(message):
    translated = _catalog.get(message)
    return translated if isinstance(translated, str) and translated else message


class Message(str):
    """English template plus arguments, retained by explicit UI bindings."""
    def __new__(cls, source, arguments=None):
        text = _lookup(source)
        if arguments:
            text = text.format(**{key: str(value) if isinstance(value, Message) else value
                                  for key, value in arguments.items()})
        instance = super().__new__(cls, text)
        instance.source = source
        instance.arguments = arguments or {}
        return instance

    def render(self):
        arguments = {key: value.render() if isinstance(value, Message) else value
                     for key, value in self.arguments.items()}
        text = _lookup(self.source)
        return text.format(**arguments) if arguments else text

    def __str__(self):
        return self.render()

    def format(self, **arguments):
        return Message(self.source, arguments)


def _(message):
    return Message(message)


def ngettext(singular, plural, count):
    """English and all supported Spanish variants use one vs. other."""
    return _(singular if count == 1 else plural)
