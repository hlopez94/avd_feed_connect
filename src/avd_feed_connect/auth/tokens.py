"""Refresh-token storage.

The refresh token is the crown jewel (``offline_access``, long-lived). It is
kept in the login keyring via libsecret when reachable (encrypted at rest by
the OS), and falls back to a ``0600`` JSON file only when the keyring can't be
used. A legacy plaintext ``token-cache.json`` written by older versions is
migrated into the keyring on first read and then removed.
"""

import json
import os
import time

from .. import config

# libsecret schema id — kept identical to older releases so tokens they stored
# are still found.
_SCHEMA_ID = "io.github.shakeelosmani.avd_feed_connect"


class TokenStore:
    """Persists a single refresh-token record: keyring first, 0600 file fallback."""

    def __init__(self, cache_path=None, account_id=None):
        self._cache = cache_path or config.CACHE
        self._schema = None  # lazily built libsecret schema
        self.account_id = account_id

    # -- keyring (libsecret) ------------------------------------------------

    def _secret(self):
        """(Secret_module, schema, attrs) if libsecret is available, else None."""
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except Exception:
            return None
        if self._schema is None:
            self._schema = Secret.Schema.new(
                _SCHEMA_ID, Secret.SchemaFlags.NONE,
                {"attr": Secret.SchemaAttributeType.STRING})
        return Secret, self._schema, {"attr": "token-cache" if self.account_id is None else "account-" + self.account_id}

    def _keyring_store(self, record_json):
        s = self._secret()
        if not s:
            return False
        Secret, schema, attrs = s
        try:
            return bool(Secret.password_store_sync(
                schema, attrs, Secret.COLLECTION_DEFAULT,
                "AVD Feed + Connect refresh token", record_json, None))
        except Exception:
            return False

    def _keyring_load(self):
        s = self._secret()
        if not s:
            return None
        Secret, schema, attrs = s
        try:
            v = Secret.password_lookup_sync(schema, attrs, None)
            return json.loads(v) if v else None
        except Exception:
            return None

    def _keyring_clear(self):
        s = self._secret()
        if not s:
            return
        Secret, schema, attrs = s
        try:
            Secret.password_clear_sync(schema, attrs, None)
        except Exception:
            pass

    # -- 0600 file fallback -------------------------------------------------

    def _shred_cache_file(self):
        try:
            os.remove(self._cache)
        except OSError:
            pass

    def _file_store(self, record):
        os.makedirs(os.path.dirname(self._cache), mode=0o700, exist_ok=True)  # XDG dir may not exist yet
        tmp = self._cache + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, self._cache)  # atomic

    # -- public API ---------------------------------------------------------

    def save(self, refresh_token):
        """Persist the refresh token (+ obtained timestamp): keyring if
        possible, else a 0600 file. A successful keyring write shreds any
        plaintext file so a copy is never left behind."""
        record = {"refresh_token": refresh_token, "obtained": int(time.time())}
        if self._keyring_store(json.dumps(record)):
            self._shred_cache_file()
            return
        self._file_store(record)

    def load(self):
        """Return ``{'refresh_token':.., 'obtained':..}`` or ``None``. Promotes
        a legacy / fallback plaintext file into the keyring (then shreds it) on
        first read."""
        rec = self._keyring_load()
        if rec and rec.get("refresh_token"):
            return rec
        try:
            with open(self._cache) as f:
                rec = json.load(f)
        except Exception:
            return None
        if rec and rec.get("refresh_token"):
            if self._keyring_store(json.dumps(rec)):
                self._shred_cache_file()
            return rec
        return None

    def has(self):
        return self.load() is not None

    def clear(self):
        self._keyring_clear()
        self._shred_cache_file()
