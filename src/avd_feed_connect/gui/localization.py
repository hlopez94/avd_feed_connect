"""Explicit GTK text bindings, leaving server/account text untouched."""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..i18n import Message, on_language_changed

# GObject weak refs follow native widget lifetime, unlike Python weak refs:
# PyGObject can discard/recreate a Python wrapper while its widget stays alive.
_widgets = {}
_models = {}
_dropdowns = {}


def _forget(references, token):
    references.pop(token, None)


def _track(widget, references):
    token = object()
    references[token] = widget.weak_ref(_forget, references, token)


def bind(widget, property_name, value):
    properties = getattr(widget, '_translation_bindings', None)
    if properties is None:
        properties = widget._translation_bindings = {}
        _track(widget, _widgets)
    if isinstance(value, Message):
        properties[property_name] = value
        value = value.render()
    else:
        properties.pop(property_name, None)
    getattr(widget, 'set_' + property_name)(value)


def create(widget_type, **properties):
    widget = widget_type(**{key: value.render() if isinstance(value, Message) else value
                            for key, value in properties.items()})
    messages = {key: value for key, value in properties.items() if isinstance(value, Message)}
    if isinstance(widget, Gtk.DropDown):
        _track(widget, _dropdowns)
    if messages:
        widget._translation_bindings = messages
        _track(widget, _widgets)
    return widget


def strings(labels):
    model = Gtk.StringList.new([str(label) for label in labels])
    model._translation_labels = labels
    _track(model, _models)
    return model


def _alive(references):
    return [widget for reference in list(references.values())
            if (widget := reference()) is not None]


def refresh_bindings():
    for widget in _alive(_widgets):
        for name, message in widget._translation_bindings.items():
            getattr(widget, 'set_' + name)(message.render())
    selections = [(dropdown, dropdown.get_selected()) for dropdown in _alive(_dropdowns)]
    for model in _alive(_models):
        model.splice(0, model.get_n_items(), [str(label) for label in model._translation_labels])
    for dropdown, selected in selections:
        dropdown.set_selected(selected)


on_language_changed(refresh_bindings)
