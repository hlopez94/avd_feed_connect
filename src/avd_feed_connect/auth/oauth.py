"""The OAuth2 sign-in flows and silent token renewal.

Mirrors client.wvd.microsoft.com's own flow: interactive authorization-code +
PKCE (Conditional Access commonly blocks the device-code flow), caching the
``offline_access`` refresh token so later runs are silent. The short-lived
access token is re-minted via the ``refresh_token`` grant whenever it expires.

Per-session state — the signed-in user's UPN and the reason the last silent
refresh failed — lives on the :class:`OAuthClient` instance, not as module
globals, so the GUI reads and updates exactly one source of truth.
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser

from ..i18n import _ as tr
from .. import config, http
from .tokens import TokenStore


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class OAuthClient:
    """Acquires access tokens for the AVD feed, interactively or silently."""

    def __init__(self, token_store=None):
        self.tokens = token_store or TokenStore()
        # UPN is normally learned from the id_token; AVD_UPN pins it (and
        # pre-fills the login hint), in which case set_upn_from_token defers.
        self.upn = os.environ.get("AVD_UPN", "")
        # Why the last silent refresh failed (AADSTS code + description), so the
        # GUI can tell the user the real reason instead of "sign in again".
        self.last_refresh_error = ""
        self.account_identity = None

    # -- interactive flows --------------------------------------------------

    def _auth_code(self):
        """Interactive authorization-code + PKCE — the flow the native client
        uses. Conditional Access frequently blocks the device-code flow but
        allows this one, so this is what avoids the 'You cannot access this
        right now' wall."""
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": config.CLIENT_ID, "response_type": "code",
            "redirect_uri": config.REDIRECT, "scope": config.SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "prompt": "select_account",
        }
        if self.upn:
            params["login_hint"] = self.upn
        url = config.LOGIN + "/authorize?" + urllib.parse.urlencode(params)
        print("\nOpen this URL in your browser and sign in:\n\n  " + url + "\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print("After sign-in the page goes blank at login.microsoftonline.com/…/"
              "nativeclient — copy that FINAL address bar URL and paste it here.\n")
        landed = input("Paste the redirected URL (or the code value): ").strip()
        # Accept: a full URL, a bare "?...=..." query, "code=..&state=..", or the
        # bare code possibly with a trailing "&state=..&session_state=.." appended.
        frag = landed
        if "://" in frag:
            p = urllib.parse.urlparse(frag)
            frag = p.query or p.fragment
        frag = frag.lstrip("?#")
        qs = urllib.parse.parse_qs(frag)
        if "error" in qs:
            sys.exit("auth error: " + (qs.get("error_description") or qs["error"])[0])
        if "code" in qs:
            code = qs["code"][0]
            returned_state = qs.get("state", [None])[0]
        else:
            # bare code; drop any trailing &state=…/&session_state=… the paste kept
            code = frag.split("&", 1)[0]
            returned_state = None
        if returned_state is not None and returned_state != state:
            sys.exit("state mismatch — aborting for safety, try again")
        if not code:
            sys.exit("no authorization code found in what you pasted")
        st, tok = http.post(config.LOGIN + "/token", {
            "grant_type": "authorization_code", "client_id": config.CLIENT_ID,
            "code": code, "redirect_uri": config.REDIRECT, "scope": config.SCOPE,
            "code_verifier": verifier})
        if st != 200:
            sys.exit(f"token exchange failed ({st}): {tok.get('error','')}: "
                     f"{tok.get('error_description','')[:300]}")
        return tok

    def _device_code(self):
        st, dc = http.post(config.LOGIN + "/devicecode",
                           {"client_id": config.CLIENT_ID, "scope": config.SCOPE})
        if st != 200:
            sys.exit(f"devicecode request failed ({st}): {dc}")
        print("\n>>> " + dc["message"] + "\n", flush=True)
        interval = dc.get("interval", 5)
        while True:
            time.sleep(interval)
            st, tok = http.post(config.LOGIN + "/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": config.CLIENT_ID, "device_code": dc["device_code"]})
            if st == 200:
                return tok
            err = tok.get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            sys.exit(f"device-code auth failed: {err}: "
                     f"{tok.get('error_description','')[:300]}")

    # -- silent renewal -----------------------------------------------------

    def _refresh(self, rt):
        st, tok = http.post(config.LOGIN + "/token", {
            "grant_type": "refresh_token", "client_id": config.CLIENT_ID,
            "scope": config.SCOPE, "refresh_token": rt})
        if st != 200:
            self.last_refresh_error = (
                f"{tok.get('error', '?')}: {tok.get('error_description', '')}")
            print(f"token refresh failed (HTTP {st}) {self.last_refresh_error[:300]}",
                  file=sys.stderr)
            return None
        self.last_refresh_error = ""
        return tok

    def set_upn_from_token(self, tok):
        """Learn the signed-in user's UPN from the id_token so /u: and the login
        hint work without hardcoding an account. Sets ``self.upn`` when it isn't
        already pinned via AVD_UPN."""
        if self.upn:
            return self.upn
        idt = tok.get("id_token")
        if not idt or idt.count(".") < 2:
            return self.upn
        try:
            payload = idt.split(".")[1]
            payload += "=" * (-len(payload) % 4)  # pad base64url
            claims = json.loads(
                base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
            self.upn = claims.get("preferred_username") or claims.get("upn") \
                or claims.get("unique_name") or claims.get("email") or ""
        except Exception:
            pass
        return self.upn

    def _check_account(self, tok, allow_missing_identity=False):
        if self.account_identity is None:
            return
        from ..accounts import claims_from_token
        if allow_missing_identity and not tok.get("id_token"):
            # Refresh responses may omit the unchanged identity token.
            return
        claims = claims_from_token(tok)
        if (claims['tid'], claims['oid']) != self.account_identity:
            raise ValueError(tr("Please sign in with the selected work account."))

    def get_token(self, verbose=True, use_device_code=False):
        """Return an access token: silent refresh if a cached refresh token
        works, otherwise an interactive sign-in."""
        rec = self.tokens.load()
        if rec:
            rt = rec["refresh_token"]
            tok = self._refresh(rt)
            if tok:
                self._check_account(tok, allow_missing_identity=True)
                self.tokens.save(tok.get("refresh_token", rt))
                self.set_upn_from_token(tok)
                if verbose:
                    print(f"token: silent refresh ok (expires_in={tok['expires_in']}s,"
                          f" rotated_refresh={'yes' if 'refresh_token' in tok else 'no'})")
                return tok["access_token"]
            if verbose:
                print("cached refresh token invalid, signing in again")
        tok = self._device_code() if use_device_code else self._auth_code()
        self._check_account(tok)
        self.tokens.save(tok["refresh_token"])
        self.set_upn_from_token(tok)
        if verbose:
            how = "device-code" if use_device_code else "interactive auth-code"
            print(f"token: {how} sign-in ok (expires_in={tok['expires_in']}s)")
        return tok["access_token"]
