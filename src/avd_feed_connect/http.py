"""Minimal HTTP helpers built on the standard library (no third-party deps in
the GNOME runtime). ``post`` is used for the OAuth token endpoints; ``get`` for
the authenticated feed/RDP downloads, which need the approved MS user-agent.
"""

import json

import urllib.error
import urllib.parse
import urllib.request

from .i18n import _ as tr
from . import config


def check_trusted(url):
    """Refuse to attach a bearer token to a non-Microsoft or non-https URL."""
    if not config.is_trusted_url(url):
        host = urllib.parse.urlparse(url).hostname
        raise ValueError(tr("Refusing to send token to untrusted URL (host: {host})").format(host=host))


def post(url, data):
    """POST form-encoded ``data`` and return ``(status, parsed_json)``.

    HTTP errors are not raised — the body is still parsed so callers can read
    the OAuth ``error`` / ``error_description`` fields.
    """
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(url, token, accept="*/*"):
    """GET ``url`` with a bearer ``token`` and the approved MS user-agent.

    Returns ``(status, headers_dict, body_text)``; HTTP errors return the error
    response rather than raising.
    """
    check_trusted(url)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", config.MS_USER_AGENT)
    req.add_header("X-MS-User-Agent", config.MS_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")
