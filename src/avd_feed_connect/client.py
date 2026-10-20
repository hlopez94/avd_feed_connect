""":class:`AvdClient` — the single object the GUI and CLI talk to.

It wires together token storage, the OAuth flows, feed discovery and the RDP
launcher, and exposes the per-session state (``upn``, ``last_refresh_error``)
that used to be module globals. Callers hold one instance and read/update its
``upn`` in one place.
"""

from .auth import OAuthClient, TokenStore
from .feed import FeedClient
from .rdp import RdpLauncher, download_rdp


class AvdClient:
    """Feed + connect operations for one signed-in session."""

    def __init__(self, token_store=None, output_dir=None):
        self.tokens = token_store or TokenStore()
        self.output_dir = output_dir
        self.oauth = OAuthClient(self.tokens)
        self.feed = FeedClient()
        self.rdp = RdpLauncher()

    # -- session state (formerly module globals) ----------------------------

    @property
    def upn(self):
        return self.oauth.upn

    @upn.setter
    def upn(self, value):
        self.oauth.upn = value

    @property
    def last_refresh_error(self):
        return self.oauth.last_refresh_error

    # -- auth ---------------------------------------------------------------

    def get_token(self, verbose=True, use_device_code=False):
        return self.oauth.get_token(verbose=verbose, use_device_code=use_device_code)

    def set_upn_from_token(self, tok):
        return self.oauth.set_upn_from_token(tok)

    def has_token(self):
        return self.tokens.has()

    def load_token_record(self):
        return self.tokens.load()

    def save_refresh_token(self, refresh_token):
        return self.tokens.save(refresh_token)

    def clear_token_cache(self):
        return self.tokens.clear()

    # -- feed / connect -----------------------------------------------------

    def enumerate_feed(self, token):
        return self.feed.enumerate(token)

    def download_rdp(self, token, res):
        return download_rdp(token, res, output_dir=self.output_dir)

    def launch(self, path):
        return self.rdp.launch(path, self.upn)
