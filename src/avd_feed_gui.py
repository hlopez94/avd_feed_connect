#!/usr/bin/env python3
"""Windows-App-style desktop client for Azure Virtual Desktop on Linux.

A GTK4 + WebKitGTK 6.0 front-end over the feed-discovery logic in the
avd_feed_connect package (see its client.AvdClient facade):
  * in-app interactive sign-in (embedded WebKit view — the same auth-code+PKCE
    flow the native client uses, so Conditional Access lets it through; it
    catches the …/oauth2/nativeclient?code=… redirect automatically, no paste).
    The modern WebKit engine renders federated org IdP pages (ADFS/Okta/Ping/…)
    that the old WebKit2GTK 4.1 used to freeze on (issue #1).
  * a tiled workspace grid with the real per-resource icons from the feed
  * double-click / Enter a tile to connect via the bundled sdl-freerdp
  * persistent session (cached workspaces + icons) + silent token refresh +
    sign out. When the tenant's Conditional Access sign-in frequency rejects
    the refresh (AADSTS70043, "every time" = 5 min), the saved grid stays,
    the status says why, and connecting re-authenticates (account picker,
    prompt=select_account — the flow that works everywhere, including tenants
    with Seamless SSO) and then connects on its own.

The connection itself is still sdl-freerdp with /gateway:type:arm /sec:aad, so
camera/mic/gfx behave exactly as before.

A tray icon (gui.tray, StatusNotifierItem over plain D-Bus — GTK4 has no tray
API) lists every resource by workspace with Connect / Connect (windowed) /
Focus window / Disconnect. Opt-in (Settings menu), while a tray host is present:
closing the window hides it to the tray, and the app can start hidden there.
AVD_TRAY=0 turns the tray off.

Runtime: PyGObject with Gtk 4.0, WebKit 6.0 (all in the GNOME 51 Flatpak
runtime).
"""

import json
import os
import re as _re
import sys
import threading
import time
import urllib.request
import urllib.parse
import hashlib
import base64
import secrets
import shutil
import signal
import subprocess
import pty
import select
import uuid

# Software-composite WebKit (our sign-in webview) instead of the DMABUF/GBM
# GPU path. That path fails to allocate a GBM buffer on some GPUs/drivers
# (notably under XWayland / in the Flatpak sandbox, and after repeated webviews
# exhaust buffers), leaving the sign-in window BLANK. Must be set before WebKit
# initializes its renderer. setdefault so a user can still force the GPU path.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

APP_ID = "io.github.shakeelosmani.avd_feed_connect"

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib  # noqa: E402
# The X11 window class comes from the program name, which is otherwise
# "python3". Use the app id so docks match the .desktop file and window
# managers can target the window with rules. Must run before GTK initializes.
GLib.set_prgname(APP_ID)
from gi.repository import Gtk, Gdk, Gio  # noqa: E402
from gi.repository import WebKit  # noqa: E402

# The feed/auth package is installed as a sibling directory next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from avd_feed_connect import config, http  # noqa: E402
from avd_feed_connect.auth.oauth import _b64url  # noqa: E402
from avd_feed_connect.client import AvdClient  # noqa: E402
from avd_feed_connect.gui.workspaces import select_backend as workspace_backend  # noqa: E402
from avd_feed_connect.rdp.launcher import (  # noqa: E402
    apply_client_hotkeys, build_display_args)
from avd_feed_connect.gui.theme import CSS_BASE, PALETTE_DARK, PALETTE_LIGHT  # noqa: E402
from avd_feed_connect.gui.demo import demo_resources  # noqa: E402
from avd_feed_connect.gui import storage, tray_menu, x11focus  # noqa: E402
from avd_feed_connect.gui.tray import TrayIcon  # noqa: E402

from avd_feed_connect.accounts import ProfileRegistry, profile_dir, claims_from_token  # noqa: E402
from avd_feed_connect.auth.tokens import TokenStore  # noqa: E402
from avd_feed_connect.gui.session import AccountSession  # noqa: E402

APP_NAME = "AVD Feed + Connect Linux"
APP_VERSION = "0.5.0"





class AvdApp(Gtk.Application):
    def __init__(self):
        # Demo/screenshot runs skip D-Bus single-instance registration entirely
        # (NON_UNIQUE), so they always run standalone and never hand off to (or
        # collide with) the installed app. Under Flatpak the sandbox only lets
        # the app own its exact app id, so a custom demo id can't be registered.
        if os.environ.get("AVD_DEMO"):
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.NON_UNIQUE)
        else:
            super().__init__(application_id=APP_ID)
        self.registry = ProfileRegistry()
        if not os.environ.get("AVD_DEMO"):
            self.registry.migrate_legacy(TokenStore())
        self.accounts = {}
        for profile in self.registry.profiles:
            self.accounts[profile['id']] = AccountSession(
                profile, self.registry.client(profile['id']),
                profile_dir(self.registry.root, profile['id']))
        self.selected_id = self.registry.selected_id
        self._adding = None
        self.win = None
        self.stack = None
        self.grid = None
        self.status = None
        self._tiles = []            # GTK4 FlowBox has no get_children(); track ours
        self._signout_item = None
        self._scale = 1             # client display scale factor (1 or 2 = HiDPI)
        self._n_monitors = 1        # how many monitors the compositor reports
        self._settings = storage.load_settings()   # application-wide defaults and appearance
        self._signin_dlg = None      # the open sign-in window, if any
        self._connecting = set()      # (account id, resource id) currently connecting
        self._tray = None             # TrayIcon, unless AVD_TRAY=0 or no session bus
        self._sessions = {}           # (account id, resource id) → connection state
        self._tray_toggles = []       # settings-menu tray options (need a tray host)
        self._start_hidden_source = 0  # timeout giving up on "start in the tray"

    # ---- app lifecycle ----------------------------------------------------
    def do_activate(self):
        if self.win:
            self.win.present()
            return
        self._build_ui()
        # "Start in the tray" only applies to a saved session (a first run must
        # show sign-in) and only once a tray host accepts the icon; otherwise
        # the window is shown after a short wait, as it would have been.
        start_hidden = bool(self._settings.get("_start_in_tray")
                            and not os.environ.get("AVD_DEMO")
                            and bool(self.current and self.current.resources))
        if not start_hidden:
            self.win.present()
        self._detect_displays()
        self._start_tray()
        if start_hidden:
            if self._tray:
                self._start_hidden_source = GLib.timeout_add_seconds(
                    3, self._start_hidden_timeout)
            else:
                self.win.present()
        # Dev/screenshot mode: show a fixed, anonymous sample feed (no network,
        # no sign-in). Used for documentation screenshots so they carry no real
        # account or org details. Never triggered in normal use.
        if os.environ.get("AVD_DEMO"):
            profile = {'id': uuid.uuid4().hex, 'tid': 'demo', 'oid': 'demo', 'upn': 'alex@contoso.com'}
            demo_session = AccountSession(profile, AvdClient(), profile_dir(self.registry.root, profile['id']))
            demo_session.client.upn = profile['upn']
            self.accounts[profile['id']] = demo_session
            self.selected_id = profile['id']
            demo = demo_resources()
            self._populate(demo)
            self._set_status("Signed in as alex@contoso.com")
            self._set_tile_state("d2", "", "state-connected")
            return
        # If we have previously discovered workspaces, show them immediately and
        # refresh the token + feed silently in the background (Windows-App-style
        # persistent session). Only a first run with no cache shows sign-in.
        if self.current:
            self._select_account(self.selected_id)
        else:
            self._show("signin")

    @property
    def current(self):
        return self.accounts.get(self.selected_id)

    def _visible(self, session):
        return session is self.current and not session.removed

    def _select_account(self, profile_id):
        session = self.accounts.get(profile_id)
        if not session or session.removed:
            return
        self.selected_id = profile_id
        if not os.environ.get("AVD_DEMO"):
            self.registry.select(profile_id)
        self._update_account(session.client.upn)
        self._rebuild_account_menu()
        self._clear_tiles()
        if session.resources:
            self._populate(session.resources, session)
        else:
            self._show("busy", session)
        self._set_connecting("", "", False, session)
        threading.Thread(target=self._background_refresh, args=(session,), daemon=True).start()

    def _rebuild_account_menu(self):
        if not hasattr(self, '_account_rows'):
            return
        row = self._account_rows.get_first_child()
        while row:
            self._account_rows.remove(row)
            row = self._account_rows.get_first_child()
        for session in self.accounts.values():
            if session.removed:
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_hexpand(True)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = session.display_name
            title = Gtk.Label(label=("✓ " if session.id == self.selected_id else "") + name, xalign=0)
            title.set_ellipsize(3)
            title.set_max_width_chars(36)
            title.add_css_class("account-name")
            email = Gtk.Label(label=session.client.upn, xalign=0)
            email.set_ellipsize(3)
            email.set_max_width_chars(36)
            email.add_css_class("account-email")
            text.append(title)
            text.append(email)
            button.set_child(text)
            button.connect("clicked", lambda _, sid=session.id: (
                self._account_pop.popdown(), self._select_account(sid)))
            edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
            edit.add_css_class("flat")
            edit.set_tooltip_text("Edit account name")
            edit.connect("clicked", lambda _, owner=session: (
                self._account_pop.popdown(), self._edit_account_name(owner)))
            row.append(button)
            row.append(edit)
            self._account_rows.append(row)
        self._account_separator.set_visible(self._account_rows.get_first_child() is not None)
        self._signout_item.set_sensitive(self.current is not None)

    def _edit_account_name(self, session):
        if session.removed:
            return
        dialog = Gtk.Window(title="Edit account name", transient_for=self.win, modal=True)
        dialog.add_css_class("avd-settings")
        dialog.set_default_size(380, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(16)
        entry = Gtk.Entry()
        entry.set_text(session.profile.get('alias', ''))
        entry.set_placeholder_text(session.display_name)
        box.append(Gtk.Label(label=session.client.upn, xalign=0))
        box.append(entry)
        box.append(Gtk.Label(label="Leave empty to use the email domain.",
                             xalign=0, wrap=True))
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.destroy())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")

        def apply(*_):
            if not session.removed:
                try:
                    self.registry.rename(session.id, entry.get_text())
                except OSError as error:
                    self._error("Could not save account name: " + str(error))
                    return
                self._rebuild_account_menu()
                if self._visible(session):
                    self._update_account(session.client.upn)
                self._tray_refresh()
            dialog.destroy()

        save.connect("clicked", apply)
        entry.connect("activate", apply)
        buttons.append(cancel)
        buttons.append(save)
        box.append(buttons)
        dialog.set_child(box)
        dialog.present()

    def _add_account(self):
        if self._adding:
            return
        profile = {'id': uuid.uuid4().hex, 'tid': '', 'oid': '', 'upn': ''}
        session = AccountSession(profile, self.registry.client(profile['id']),
                                 profile_dir(self.registry.root, profile['id']))
        session.client.upn = ""
        self._adding = session
        self._interactive_signin(session)

    def _discard_add(self, session):
        if session is self._adding:
            session.close()
            session.web_session = None
            shutil.rmtree(session.directory, ignore_errors=True)
            self._adding = None

    def _detect_displays(self):
        """Read the client's real display layout from the compositor so the
        remote session matches it automatically (like the Windows App does):
        number of monitors → single vs multi-monitor, and the HiDPI scale."""
        try:
            mons = Gdk.Display.get_default().get_monitors()
            self._n_monitors = max(1, mons.get_n_items())
        except Exception:
            mons, self._n_monitors = None, 1
        try:
            if self.win.get_mapped() or not mons:
                self._scale = self.win.get_scale_factor() or 1
            else:   # started hidden in the tray: no window yet, ask the monitor
                self._scale = mons.get_item(0).get_scale_factor() or 1
        except Exception:
            self._scale = 1

    def _background_refresh(self, session):
        if not self._do_refresh(True, session):
            if session.resources:
                self._set_status(self._refresh_failed_status(session), session)
            else:
                self._show("signin", session)
            return
        self._load_feed(session)

    def _is_dark(self):
        ov = os.environ.get("AVD_THEME", "").strip().lower()
        if ov in ("dark", "light"):
            return ov == "dark"
        pref = (self._settings.get("_theme") or "system")   # in-app choice
        if pref == "light":
            return False
        if pref == "dark":
            return True
        s = Gtk.Settings.get_default()
        try:
            if s.get_property("gtk-application-prefer-dark-theme"):
                return True
        except Exception:
            pass
        try:
            return "dark" in (s.get_property("gtk-theme-name") or "").lower()
        except Exception:
            return False

    def _apply_theme(self, *_):
        css = (PALETTE_DARK if self._is_dark() else PALETTE_LIGHT) + CSS_BASE
        self._css_provider.load_from_string(css)

    def _set_theme(self, key):
        """Persist the appearance choice (system/light/dark) and apply it live."""
        self._settings["_theme"] = key
        storage.save_settings(self._settings)
        self._apply_theme()

    def _build_ui(self):
        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._apply_theme()
        st = Gtk.Settings.get_default()
        for prop in ("notify::gtk-application-prefer-dark-theme", "notify::gtk-theme-name"):
            st.connect(prop, self._apply_theme)

        self.win = Gtk.ApplicationWindow(application=self, title=APP_NAME)
        self.win.add_css_class("avd")
        self.win.set_default_size(820, 600)
        self.win.connect("close-request", self._on_close_request)
        self.win.connect("notify::is-active", self._on_window_active_changed)
        outside = Gtk.GestureClick()
        outside.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        outside.connect("pressed", self._on_menu_outside_pressed)
        self.win.add_controller(outside)

        # ---- header bar: brand (left) · refresh + account (right) -----------
        hb = Gtk.HeaderBar()
        hb.add_css_class("avd-header")
        hb.set_title_widget(Gtk.Label())     # blank the centered window title
        self.win.set_titlebar(hb)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        # The mark IS the image (a Gtk.Image always draws its icon centered in
        # its allocation), sized/tinted by the .mark CSS — no box wrapper to
        # mis-align or expand.
        mark = Gtk.Image.new_from_icon_name("computer-symbolic")
        mark.set_pixel_size(18); mark.add_css_class("mark"); mark.set_valign(Gtk.Align.CENTER)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tbox.set_valign(Gtk.Align.CENTER)      # center the title block next to the logo
        t1 = Gtk.Label(label="AVD Feed + Connect", xalign=0); t1.add_css_class("brand-title")
        t2 = Gtk.Label(label="Azure Virtual Desktop · Windows 365", xalign=0)
        t2.add_css_class("brand-sub")
        tbox.append(t1); tbox.append(t2)
        brand.append(mark); brand.append(tbox)
        brand.set_valign(Gtk.Align.CENTER)
        hb.pack_start(brand)

        # account menu button (avatar + who + caret), opens the saved account list
        menu_btn = Gtk.MenuButton(); menu_btn.add_css_class("acct-btn")
        acctbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # The avatar IS the label (a Gtk.Label centers its own text), sized/
        # tinted by the .avatar CSS — no box wrapper.
        self._avatar_lbl = Gtk.Label(label="?"); self._avatar_lbl.add_css_class("avatar")
        self._avatar_lbl.set_valign(Gtk.Align.CENTER)
        self._acct_who = Gtk.Label(label="Sign in"); self._acct_who.add_css_class("acct-who")
        self._acct_who.set_ellipsize(3); self._acct_who.set_max_width_chars(24)
        caret = Gtk.Image.new_from_icon_name("pan-down-symbolic"); caret.add_css_class("caret")
        acctbox.append(self._avatar_lbl); acctbox.append(self._acct_who); acctbox.append(caret)
        menu_btn.set_child(acctbox)

        # Avoid an input grab that can deactivate the parent on touch input.
        # Outside presses and parent deactivation dismiss these menus explicitly.
        pop = Gtk.Popover(autohide=False)
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom", "start", "end"):
            getattr(pbox, f"set_margin_{m}")(6)

        # Appearance: System / Light / Dark (remembered), applied live.
        appr = Gtk.Label(label="APPEARANCE", xalign=0); appr.add_css_class("menu-head")
        appr.set_margin_start(4); appr.set_margin_top(2)
        pbox.append(appr)
        seg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        seg.add_css_class("linked"); seg.set_margin_bottom(4)
        cur_theme = self._settings.get("_theme") or "system"
        first = None
        for key, label in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            b = Gtk.ToggleButton(label=label)
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.set_active(key == cur_theme)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_theme(k))
            seg.append(b)
        pbox.append(seg)
        pbox.append(Gtk.Separator())

        # System tray options (off by default, so closing still quits unless
        # asked). Insensitive until a tray host shows our icon.
        trayh = Gtk.Label(label="SYSTEM TRAY", xalign=0); trayh.add_css_class("menu-head")
        trayh.set_margin_start(4); trayh.set_margin_top(2)
        pbox.append(trayh)
        for key, label in (("_close_to_tray", "Keep running in the tray when closed"),
                           ("_start_in_tray", "Start in the tray")):
            cb = Gtk.CheckButton(label=label)
            cb.set_active(bool(self._settings.get(key)))
            cb.set_sensitive(False)
            cb.set_tooltip_text("Needs a system tray on your desktop")
            cb.connect("toggled", lambda btn, k=key: self._set_tray_option(k, btn.get_active()))
            pbox.append(cb)
            self._tray_toggles.append(cb)
        pbox.append(Gtk.Separator())

        setbtn = Gtk.Button(label="Default settings…"); setbtn.add_css_class("flat")
        setbtn.connect("clicked", lambda *_: (settings_pop.popdown(), self._open_settings(None)))
        pbox.append(setbtn)
        settings_pop = pop
        settings_pop.add_css_class("avd-menu")
        self._settings_pop = settings_pop
        settings_btn = Gtk.MenuButton(icon_name="emblem-system-symbolic")
        settings_btn.add_css_class("iconbtn")
        settings_btn.set_tooltip_text("Settings")
        settings_pop.set_child(pbox)
        settings_btn.set_popover(settings_pop)
        # Avoid an input grab that can deactivate the parent on touch input.
        # Outside presses and parent deactivation dismiss these menus explicitly.
        pop = Gtk.Popover(autohide=False)
        self._account_pop = pop
        pop.add_css_class("avd-menu")
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(pbox, f"set_margin_{margin}")(6)
        self._account_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pbox.append(self._account_rows)
        self._account_separator = Gtk.Separator()
        pbox.append(self._account_separator)
        add = Gtk.Button(label="Add account")
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: (pop.popdown(), self._add_account()))
        pbox.append(add)
        pbox.append(Gtk.Separator())
        signout = Gtk.Button(label="Sign out"); signout.add_css_class("flat")
        signout.set_sensitive(False)
        signout.connect("clicked", lambda *_: (pop.popdown(), self._sign_out()))
        self._signout_item = signout
        pbox.append(signout)
        pop.set_child(pbox)
        menu_btn.set_popover(pop)

        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.add_css_class("iconbtn")
        self.refresh_btn.set_tooltip_text("Refresh workspaces")
        self.refresh_btn.connect("clicked", lambda *_: self._reload_feed())

        hb.pack_end(menu_btn)
        hb.pack_end(settings_btn)
        self._rebuild_account_menu()
        hb.pack_end(self.refresh_btn)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_css_class("avd-page")
        self.win.set_child(self.stack)

        # ---- sign-in hero ---------------------------------------------------
        signin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        signin.add_css_class("avd-page")
        signin.set_valign(Gtk.Align.CENTER); signin.set_halign(Gtk.Align.CENTER)
        bm = Gtk.Image.new_from_icon_name("computer-symbolic"); bm.set_pixel_size(32)
        bm.add_css_class("bigmark"); bm.set_margin_bottom(14); bm.set_halign(Gtk.Align.CENTER)
        h1 = Gtk.Label(label="Sign in to your workspaces"); h1.add_css_class("hero-title")
        h2 = Gtk.Label(label="Connect your Microsoft work account to see the desktops "
                             "and apps you're entitled to.")
        h2.add_css_class("hero-sub"); h2.set_wrap(True); h2.set_justify(Gtk.Justification.CENTER)
        h2.set_max_width_chars(38); h2.set_margin_top(4)
        btn = Gtk.Button(label="Sign in with Microsoft"); btn.add_css_class("msbtn")
        btn.set_halign(Gtk.Align.CENTER); btn.set_margin_top(20)
        btn.connect("clicked", lambda *_: self._interactive_signin())
        note = Gtk.Label(label="Unofficial client · not affiliated with or endorsed by Microsoft")
        note.add_css_class("hero-note"); note.set_margin_top(24)
        for w in (bm, h1, h2, btn, note):
            signin.append(w)
        self.stack.add_named(signin, "signin")

        # ---- workspaces page ------------------------------------------------
        wp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wp.add_css_class("avd-page")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        for m, v in (("top", 18), ("start", 22), ("end", 22), ("bottom", 4)):
            getattr(head, f"set_margin_{m}")(v)
        sh = Gtk.Label(label="YOUR WORKSPACES", xalign=0); sh.add_css_class("section-h")
        sh.set_hexpand(True)
        self._count_lbl = Gtk.Label(label="", xalign=1); self._count_lbl.add_css_class("count")
        head.append(sh); head.append(self._count_lbl)
        wp.append(head)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        self.grid = Gtk.FlowBox(valign=Gtk.Align.START, max_children_per_line=5,
                                min_children_per_line=2, row_spacing=14,
                                column_spacing=14, homogeneous=True,
                                selection_mode=Gtk.SelectionMode.NONE)
        self.grid.set_margin_top(8); self.grid.set_margin_bottom(18)
        self.grid.set_margin_start(22); self.grid.set_margin_end(22)
        self.grid.connect("child-activated", self._on_tile_activated)
        sw.set_child(self.grid)

        # Connecting overlay: centered spinner card shown while a session is
        # establishing (silent AAD + RDP handshake), until the desktop logs on.
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(sw)
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        cbox.set_halign(Gtk.Align.CENTER); cbox.set_valign(Gtk.Align.CENTER)
        cbox.add_css_class("connect-card")
        self._connect_spinner = Gtk.Spinner(); self._connect_spinner.set_size_request(42, 42)
        self._connect_label = Gtk.Label(label="Connecting…"); self._connect_label.add_css_class("connect-label")
        csub = Gtk.Label(label="Securing your session"); csub.add_css_class("connect-sub")
        cbox.append(self._connect_spinner); cbox.append(self._connect_label); cbox.append(csub)
        cbox.set_visible(False)
        cbox.set_can_target(False)
        self._connect_overlay = cbox
        overlay.add_overlay(cbox)
        wp.append(overlay)

        # footer: live dot · status · version
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        foot.add_css_class("foot")
        self._livedot = Gtk.Box(); self._livedot.add_css_class("livedot")
        self._livedot.set_valign(Gtk.Align.CENTER)
        self.status = Gtk.Label(label="", xalign=0); self.status.add_css_class("status")
        self.status.set_hexpand(True)
        ver = Gtk.Label(label="v" + APP_VERSION, xalign=1); ver.add_css_class("ver")
        foot.append(self._livedot); foot.append(self.status); foot.append(ver)
        wp.append(foot)
        self.stack.add_named(wp, "workspaces")

        # busy page
        busy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        busy.add_css_class("avd-page")
        busy.set_valign(Gtk.Align.CENTER)
        sp = Gtk.Spinner(); sp.start()
        busy.append(sp)
        busy.append(Gtk.Label(label="Loading…"))
        self.stack.add_named(busy, "busy")
        self.stack.set_visible_child_name("busy")

    # ---- helpers (main thread) --------------------------------------------
    def _dismiss_menus(self):
        for name in ("_account_pop", "_settings_pop"):
            popover = getattr(self, name, None)
            if popover is not None:
                popover.popdown()

    def _on_menu_outside_pressed(self, _gesture, _count, x, y):
        target = self.win.pick(x, y, Gtk.PickFlags.DEFAULT)
        while target is not None:
            # Let menu items and the menu buttons handle their own interaction.
            if isinstance(target, (Gtk.Popover, Gtk.MenuButton)):
                return
            target = target.get_parent()
        self._dismiss_menus()

    def _on_window_active_changed(self, window, _property):
        """Dismiss menus when their non-grabbing parent loses activation."""
        if not window.is_active():
            self._dismiss_menus()

    def _set_status(self, text, session=None):
        GLib.idle_add(lambda: self.status.set_text(text) if self.status and (session is None or self._visible(session)) else None)

    def _update_account(self, who):
        """Fill the header account chip: email + a 2-letter avatar."""
        if not who or "@" not in who:
            self._acct_who.set_text("Sign in")
            self._avatar_lbl.set_text("?")
            return
        self._acct_who.set_text(self.current.display_name if self.current else who)
        user = who.split("@", 1)[0]
        parts = _re.split(r"[.\-_]+", user)
        initials = (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else user[1:2]))
        self._avatar_lbl.set_text(initials.upper() or "?")

    def _set_connecting(self, res_id, title, on, session=None):
        """Show/hide the centered 'Connecting…' spinner overlay. Safe to call
        from any thread. The overlay stays up while any session is connecting."""
        session = session or self.current
        if session is None:
            return
        key = (session.id, res_id)
        def apply():
            if on and not session.removed:
                self._connecting.add(key)
            else:
                self._connecting.discard(key)
            visible = [key for key in self._connecting if key[0] == self.selected_id]
            if visible:
                n = len(visible)
                self._connect_label.set_text(
                    f"Connecting to {title}…" if n == 1 else f"Connecting… ({n})")
                self._connect_spinner.start()
                self._connect_overlay.set_visible(True)
            else:
                self._connect_overlay.set_visible(False)
                self._connect_spinner.stop()
            return False
        GLib.idle_add(apply)

    def _show(self, name, session=None):
        def apply():
            if session is not None and not self._visible(session):
                return False
            self.stack.set_visible_child_name(name)
            # "Sign out" only makes sense once we're signed in (workspaces view)
            if self._signout_item is not None:
                self._signout_item.set_sensitive(self.current is not None)
        GLib.idle_add(apply)

    # ---- auth / token lifecycle ------------------------------------------
    def _schedule_refresh(self, session):
        if session.removed:
            return False
        if session.refresh_source:
            GLib.source_remove(session.refresh_source)
        secs = max(60, int(session.deadline - time.monotonic()) - 300)
        session.refresh_source = GLib.timeout_add_seconds(secs, self._auto_refresh, session)
        return False

    def _auto_refresh(self, session):
        session.refresh_source = 0
        threading.Thread(target=self._do_refresh, args=(True, session), daemon=True).start()
        return False

    def _do_refresh(self, silent, session):
        if session.refresh():
            GLib.idle_add(self._save_refreshed_profile, session)
            GLib.idle_add(self._schedule_refresh, session)
            return True
        if not silent:
            self._show("signin", session)
        return False

    def _save_refreshed_profile(self, session):
        if not session.removed:
            self.registry.save()
            self._rebuild_account_menu()
        return False

    def _ensure_token(self, session):
        with session.refresh_lock:
            if session.removed:
                return False
            if session.token and time.monotonic() < session.deadline - 120:
                return True
        return self._do_refresh(True, session)

    def _refresh_failed_status(self, session):
        err = session.client.last_refresh_error
        if "AADSTS70043" in err or "sign-in frequency" in err:
            return ("Showing saved workspaces · your organization requires signing "
                    "in again (Conditional Access sign-in frequency)")
        return "Showing saved workspaces · sign in again to refresh"

    def _web_session(self, session):
        """Shared, PERSISTENT WebKit network session used by every Microsoft
        sign-in webview — the launcher's sign-in dialog and the connection-time
        AAD resolver. It only ever visits Entra login domains, so persisting its
        cookies (a) makes a re-auth after a token refresh is refused a
        click-through (SSO) instead of full password+MFA, and (b) lets the
        connection-time gateway token be fetched silently from the same SSO
        session. Stored in our app data dir (user-private); the refresh token
        itself lives in the keyring, not here."""
        if session.web_session is None:
            d = os.path.join(session.directory, "webview")
            os.makedirs(d, mode=0o700, exist_ok=True)
            session.web_session = WebKit.NetworkSession.new(
                d, os.path.join(d, "cache"))
            try:
                session.web_session.get_cookie_manager().set_persistent_storage(
                    os.path.join(d, "cookies.sqlite"),
                    WebKit.CookiePersistentStorage.SQLITE)
            except Exception:
                pass
        return session.web_session

    def _interactive_signin(self, session=None):
        session = session or self.current
        if session is None:
            self._add_account()
            return
        if session.removed:
            return
        # Never reuse or resume a previous sign-in window: tear any existing one
        # down and start fresh, so closing and reopening is always a clean slate
        # (the user's rule: close must close, reopen must resume from clean).
        if self._signin_dlg is not None:
            old, self._signin_dlg = self._signin_dlg, None
            old.destroy()
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)
        # Always show the account picker (prompt=select_account). It is the
        # known-good flow: it bypasses Azure AD Seamless SSO — which needs a
        # Windows Kerberos ticket and, on Linux, hangs the webview forever on
        # "Trying to sign you in" — and it does NOT force the full interactive
        # re-login that makes tenants inject a security-info registration
        # interrupt ("Keep your account secure"). Trying to save the one picker
        # click (login_hint without a prompt, or prompt=login) reintroduced
        # both of those bugs, so we don't.
        params = {
            "client_id": config.CLIENT_ID, "response_type": "code",
            "redirect_uri": config.REDIRECT, "scope": config.SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "prompt": "select_account",
        }
        if session.client.upn:
            params["login_hint"] = session.client.upn   # preselect the known account in the picker
        url = config.LOGIN + "/authorize?" + urllib.parse.urlencode(params)

        # With the main window hidden in the tray (e.g. a tray-initiated
        # connect), show the sign-in on its own instead of surfacing the app:
        # a standalone top-level, since a modal dialog needs a visible parent.
        parent = self.win if self.win and self.win.get_visible() else None
        dlg = Gtk.Window(title="Sign in", transient_for=parent, modal=parent is not None)
        dlg.set_default_size(520, 640)
        # Shared PERSISTENT session (SSO cookies kept on disk) so a re-auth is a
        # click-through and the connection-time token can be fetched silently.
        # The half-finished-flow / ghost-window problems that made us try an
        # ephemeral session are fixed independently (popup blocking + explicit
        # close-request + always destroying/rebuilding the dialog + DMABUF off),
        # so persistence is safe and is what actually reduces sign-in prompts.
        wv = WebKit.WebView(network_session=self._web_session(session))
        self._apply_ua(wv)   # account sign-in → Default UA (issue #28)
        dlg.set_child(wv)
        self._signin_dlg = dlg
        got_code = [False]

        # Block popups. A page (e.g. the personal-account "approve on your phone"
        # step) can call window.open(); WebKit would spawn a separate, orphan
        # WebView/window with no controls — the "ghost window" that can't be
        # closed. Returning None here declines the popup instead.
        wv.connect("create", lambda *_a: None)

        def on_closed(*_):
            # Clicking the window's × always closes it and fully resets state,
            # so the next sign-in is a clean start (no resumed half-finished flow).
            if self._signin_dlg is dlg:
                self._signin_dlg = None
            if not got_code[0]:
                session.pending_launch = None
                self._discard_add(session)
        dlg.connect("destroy", on_closed)
        # Default close-request already destroys the window; make it explicit and
        # unconditional so nothing can swallow the close.
        dlg.connect("close-request", lambda w: (w.destroy(), True)[1])

        def on_decide(view, decision, dtype):
            if dtype != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                return False
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(config.REDIRECT) and "code=" in uri:
                decision.ignore()
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
                got_code[0] = True
                dlg.destroy()
                if qs.get("state", [state])[0] != state:
                    session.pending_launch = None
                    self._discard_add(session)
                    self._error("Sign-in state mismatch, please retry.")
                    return True
                code = qs.get("code", [""])[0]
                self._show("busy", session)
                threading.Thread(target=self._exchange, args=(code, verifier, session),
                                 daemon=True).start()
                return True
            return False

        wv.connect("decide-policy", on_decide)
        wv.load_uri(url)
        dlg.present()

    def _exchange(self, code, verifier, session):
        try:
            st, tok = http.post(config.LOGIN + "/token", {
                "grant_type": "authorization_code", "client_id": config.CLIENT_ID,
                "code": code, "redirect_uri": config.REDIRECT, "scope": config.SCOPE,
                "code_verifier": verifier})
            if st != 200:
                raise ValueError(tok.get("error_description", tok.get("error", "Sign-in failed")))
            claims_from_token(tok)
        except Exception as error:
            GLib.idle_add(self._signin_failed, session, str(error))
            return
        GLib.idle_add(self._finish_signin, session, tok)

    def _signin_failed(self, session, message):
        session.pending_launch = None
        self._discard_add(session)
        self._error("Sign-in failed: " + message)
        if self._visible(session):
            self._show("workspaces" if session.resources else "signin", session)
        return False

    def _finish_signin(self, session, tok):
        if session.removed:
            return False
        try:
            claims = claims_from_token(tok)
            if session is self._adding:
                existing = next((s for s in self.accounts.values() if not s.removed and
                                 (s.profile['tid'], s.profile['oid']) == (claims['tid'], claims['oid'])), None)
                target = existing or session
                if not target.apply_token(tok):
                    raise ValueError("Please use the selected work account.")
                profile = self.registry.upsert(claims, target.id)
                target.profile = profile
                if existing:
                    self._discard_add(session)
                else:
                    self._adding = None
                    self.accounts[target.id] = target
                session = target
            else:
                if not session.apply_token(tok):
                    raise ValueError("Please sign in with the selected work account.")
                self.registry.save()
            self._schedule_refresh(session)
            self._select_account(session.id)
        except (ValueError, OSError) as error:
            self._signin_failed(session, str(error))
        return False

    def _sign_out(self):
        session = self.current
        if session is None:
            return
        session.close()
        if session.refresh_source:
            GLib.source_remove(session.refresh_source)
            session.refresh_source = 0
        self.registry.remove(session.id)
        if session.web_session is not None:
            session.web_session.get_website_data_manager().clear(
                WebKit.WebsiteDataTypes.ALL, 0, None, None, None)
            session.web_session = None
        shutil.rmtree(session.directory, ignore_errors=True)
        self.selected_id = self.registry.selected_id
        self._clear_tiles()
        self._rebuild_account_menu()
        if self.current:
            self._select_account(self.selected_id)
        else:
            self._update_account("")
            self._show("signin")
            self._set_connecting("", "", False, session)

    def _reload_feed(self):
        session = self.current
        if not session or not session.token:
            self._interactive_signin(session)
            return
        self._load_feed_bg(session)

    def _load_feed_bg(self, session):
        self._show("busy", session)
        threading.Thread(target=self._load_feed, args=(session,), daemon=True).start()

    def _load_feed(self, session):
        if not self._ensure_token(session):
            if not session.resources:
                self._show("signin", session)
            return
        try:
            resources = session.client.enumerate_feed(session.token)
        except Exception as error:
            session.pending_launch = None
            if session.resources:
                self._show("workspaces", session)
                self._set_status("Couldn't refresh workspaces — showing saved list", session)
            else:
                self._show("signin", session)
                if self._visible(session):
                    self._error("Feed error: " + str(error))
            return
        if session.save_resources(resources):
            GLib.idle_add(self._populate, resources, session)

    def _clear_tiles(self):
        for ch in self._tiles:
            self.grid.remove(ch)
        self._tiles = []
        self._tray_refresh()

    def _populate(self, resources, session=None):
        session = session or self.current
        if session is None or session.removed:
            return False
        if not self._visible(session):
            self._rebuild_account_menu()
            self._tray_refresh()
            return False
        session.resources = resources
        self._clear_tiles()
        for res in resources:
            ch = session.tiles.get(res["id"])
            if ch is None:
                ch = self._make_tile(res, session)
                session.tiles[res["id"]] = ch
            ch._res = res
            self.grid.append(ch)
            self._tiles.append(ch)
        who = session.client.upn or "your account"
        self.status.set_text(f"Signed in as {who}")
        n = len(resources)
        self._count_lbl.set_text(f"{n} resource" + ("" if n == 1 else "s"))
        self._update_account(who)
        self._rebuild_account_menu()
        self._tray_refresh()
        self.stack.set_visible_child_name("workspaces")
        if self._signout_item is not None:
            self._signout_item.set_sensitive(True)
        # Icons: cached PNGs show immediately; with a token they're re-fetched
        # (and re-cached) in the background so the grid never looks "reset".
        threading.Thread(target=self._load_icons, args=(resources, session, list(self._tiles)),
                         daemon=True).start()
        # A connect that had to wait for sign-in resumes now, on the fresh tiles
        pending, session.pending_launch = session.pending_launch, None
        if pending:
            ch = self._child_for(pending[0])
            if ch:
                self._on_tile_activated(self.grid, ch, windowed=pending[1])

    def _make_tile(self, res, session):
        is_desktop = res["type"] == "Desktop"
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        tile.add_css_class("tile")

        # top row: tinted icon plate (left) + status pill (top-right)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        ic = Gtk.Box(); ic.add_css_class("ic")
        ic.add_css_class("desktop" if is_desktop else "app")
        ic.set_halign(Gtk.Align.START); ic.set_valign(Gtk.Align.CENTER)
        ic.set_size_request(52, 52)          # fixed square, so the glyph centers
        img = Gtk.Image.new_from_icon_name(
            "computer-symbolic" if is_desktop else "view-grid-symbolic")
        img.set_pixel_size(26)
        img.set_hexpand(True); img.set_vexpand(True)
        img.set_halign(Gtk.Align.CENTER); img.set_valign(Gtk.Align.CENTER)
        ic.append(img)
        pill = Gtk.Label(label="")
        pill.add_css_class("status-pill")
        pill.set_valign(Gtk.Align.START)
        pill.set_visible(False)
        spacer = Gtk.Box(); spacer.set_hexpand(True)
        top.append(ic); top.append(spacer); top.append(pill)

        # meta: title + type chip
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        title = Gtk.Label(label=res["title"], xalign=0); title.add_css_class("tname")
        title.set_wrap(True); title.set_max_width_chars(18); title.set_lines(2)
        title.set_ellipsize(3)
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        if not is_desktop:
            chip.add_css_class("chip-app")
        chip.set_halign(Gtk.Align.START)
        dot = Gtk.Box(); dot.add_css_class("chipdot"); dot.set_valign(Gtk.Align.CENTER)
        clabel = Gtk.Label(label="DESKTOP" if is_desktop else "REMOTE APP")
        clabel.add_css_class("chiplabel")
        chip.append(dot); chip.append(clabel)
        meta.append(title); meta.append(chip)

        tile.append(top); tile.append(meta)

        child = Gtk.FlowBoxChild()
        child.set_child(tile)
        child._account = session
        child._res = res
        child._img = img
        child._tile = tile
        child._pill = pill
        child._proc = None
        child._launching = False
        child.set_tooltip_text(
            f"{res['title']} — {res['tenant']}\nDouble-click to connect · "
            f"right-click for this workspace's settings")
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", lambda g, n, x, y, r=res: self._open_settings(r, session))
        child.add_controller(rclick)
        return child

    def _child_for(self, res_id, session=None):
        session = session or self.current
        return session.tiles.get(res_id) if session else None

    def _set_tile_state(self, res_id, text, css, session=None):
        session = session or self.current
        if session is None:
            return
        key = (session.id, res_id)
        # Render session state as a corner pill: amber "Connecting", green
        # "Connected", hidden when idle. (css: state-connecting/connected/ended)
        def apply():
            if css in ("state-connecting", "state-connected"):
                self._sessions[key] = css[len("state-"):]
            else:
                self._sessions.pop(key, None)
            self._tray_refresh()
            ch = self._child_for(res_id, session)
            if not ch:
                return
            pill, tile = ch._pill, ch._tile
            pill.remove_css_class("pill-connecting")
            tile.remove_css_class("tile-connected")
            if css == "state-connected":
                pill.set_text("CONNECTED")
                pill.set_visible(True)
                tile.add_css_class("tile-connected")
            elif css == "state-connecting":
                pill.set_text("CONNECTING")
                pill.add_css_class("pill-connecting")
                pill.set_visible(True)
            else:
                pill.set_visible(False)
        GLib.idle_add(apply)

    def _load_icons(self, resources, session, tiles):
        children = {c._res["id"]: c for c in tiles}
        token = session.token
        for res in resources:
            if session.removed:
                return
            url = res.get("icon32")
            if not url:
                continue
            path = storage.icon_path(res["id"], session.directory)
            data = None
            if token:
                try:
                    data = storage.bearer_bytes(url, token)
                    with session.refresh_lock:
                        if session.removed:
                            return
                        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                except Exception:
                    data = None
            if data is None:            # no token, or the fetch failed → cache
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
            try:
                # Load the PNG straight into a GdkTexture (the Image scales it to
                # its pixel size); avoids the deprecated new_for_pixbuf path.
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except Exception:
                continue
            ch = children.get(res["id"])
            if ch:
                GLib.idle_add(lambda c=ch, t=tex: c._img.set_from_paintable(t) if self._visible(session) else None)

    # ---- launch + live connection state -----------------------------------
    def _on_tile_activated(self, flowbox, child, windowed=False):
        session = child._account
        if session.removed:
            return
        res = child._res
        # Guard synchronously on the main thread: the second click of a
        # double-click arrives before the launch thread has set child._proc, so
        # a flag set here (not the proc handle) is what prevents a second launch.
        if getattr(child, "_launching", False) or (child._proc and child._proc.poll() is None):
            self._set_status(f"{res['title']} is already open", session)
            return
        child._launching = True
        self._set_tile_state(res["id"], "● Connecting…", "state-connecting", session)
        self._set_status(f"Connecting to {res['title']}…", session)
        self._set_connecting(res["id"], res["title"], True, session)
        threading.Thread(target=self._launch, args=(child, res, windowed, session),
                         daemon=True).start()

    def _launch(self, child, res, windowed, session):
        if not self._ensure_token(session):
            # Session lapsed and can't refresh silently — re-auth on demand
            # (like the Windows App when you click a workspace after a while),
            # keeping the workspace list intact.
            child._launching = False
            self._set_connecting(res["id"], res["title"], False, session)
            self._set_tile_state(res["id"], "", None, session)
            self._set_status("Sign in to connect", session)
            session.pending_launch = (res["id"], windowed)   # connect automatically afterwards
            GLib.idle_add(self._interactive_signin, session)
            return
        try:
            with session.refresh_lock:
                if session.removed:
                    child._launching = False
                    return
                path = session.client.download_rdp(session.token, res)
        except (SystemExit, Exception) as e:  # never let a write/HTTP error hang the tile
            child._launching = False
            self._set_connecting(res["id"], res["title"], False, session)
            self._error(str(e))
            self._set_tile_state(res["id"], "● Failed", "state-ended", session)
            return
        # Host pools not set up for Entra ID RDP auth (their feed .rdp lacks
        # "enablerdsaadauth:i:1") reject /sec:aad with HYBRID_REQUIRED_BY_SERVER
        # (issue #3), so use /sec:nla there and let FreeRDP prompt for the account
        # credentials itself. NLA against a pure Entra-joined host still can't
        # succeed from Linux (PKU2U needs the Windows CloudAP/PRT), but a
        # hybrid-AD-joined host works with on-prem-AD credentials. The gateway
        # keeps using the Entra token either way.
        try:
            rdp_text = open(path).read()
        except OSError:
            rdp_text = ""
        nla = "enablerdsaadauth:i:1" not in rdp_text.lower()
        env = dict(os.environ)
        # sdl-freerdp (SDL3) and FreeRDP's own AAD webview are unstable on native
        # Wayland (#2: "Error 71 dispatching to Wayland display"). Force X11 /
        # XWayland for the child, which is stable — and drop WAYLAND_DISPLAY so
        # nothing in the subprocess re-selects Wayland.
        env["GDK_BACKEND"] = "x11"
        env["SDL_VIDEODRIVER"] = "x11"
        env.pop("WAYLAND_DISPLAY", None)
        # FreeRDP no longer has its own webview (we resolve AAD in-process), but
        # keep this set for the child in case any bundled component initializes
        # WebKitGTK — the DMABUF/GBM path blanks on some GPUs. Harmless otherwise.
        env["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
        # Reduce tearing in the SDL3 client (vsync + double buffer). SDL hints,
        # ignored where unsupported, so always safe.
        env.setdefault("SDL_RENDER_VSYNC", "1")
        env.setdefault("SDL_VIDEO_DOUBLE_BUFFER", "1")
        if config.SDL_LIBS and os.path.isdir(config.SDL_LIBS):
            env["LD_LIBRARY_PATH"] = config.SDL_LIBS + (
                os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        safe = _re.sub(r"[^A-Za-z0-9]+", "_", res["title"])[:40]
        logpath = os.path.join(session.client.output_dir, f"session_{safe}_{hashlib.sha256(res['id'].encode()).hexdigest()[:16]}.log")
        argv = [config.SDL, path, "/gateway:type:arm", "/sec:nla" if nla else "/sec:aad"]
        if session.client.upn:
            argv.append(f"/u:{session.client.upn}")
        # Remote scale follows the client's display scale (HiDPI → 200%, standard/
        # ultrawide → 100%); AVD_SCALE overrides. Multi-monitor and any other flag
        # are opt-in via AVD_EXTRA_ARGS (e.g. "/multimon /gfx"), until a settings UI.
        # --- display config: env var > per-resource/default setting > auto ---
        extra = self._eff_extra(res, session)
        if windowed:
            # A resizable window instead of /f: build_display_args drops the
            # fullscreen/smart-sizing/multimon flags and the feed's screen mode.
            extra = (extra + " /dynamic-resolution").strip()
        # AVD NLA host pools reject FreeRDP's default "AzureAD" domain — both
        # reporters on issue #3 connect only with the domain cleared. Send an
        # empty domain so the user just types their password (a rare pool that
        # needs "AzureAD" can override with /d: in Advanced flags).
        if nla and "/d:" not in extra:
            argv.append("/d:")
        scale = os.environ.get("AVD_SCALE") or self._eff("scale", res, session) \
            or str(100 * max(1, self._scale))
        # Pin the host cert on first use by default; only turn the check off if
        # the user opted out (e.g. a pooled host pool whose certs rotate). Env
        # override: AVD_CERT=ignore / verify.
        cert_mode = os.environ.get("AVD_CERT", "").strip().lower() \
            or self._eff("cert", res, session)
        cert_flag = "/cert:ignore" if cert_mode == "ignore" else "/cert:tofu"
        apply_client_hotkeys(self._eff("hotkeys", res, session))
        argv += ["/sound:sys:pulse", "/microphone", cert_flag,
                 f"/scale-desktop:{scale}", "/log-level:info",
                 # bandwidth/quality + resilience + keepalive:
                 "+compression", "+fonts",
                 "+auto-reconnect", "/auto-reconnect-max-retries:10",
                 # inject fake input so the Azure gateway doesn't idle-drop the
                 # session (which would otherwise force a reconnect + token re-mint)
                 "/prevent-session-lock:120"]
        mm = os.environ.get("AVD_MULTIMON", "").strip().lower()
        if mm in ("1", "on", "true", "yes"):
            want_multimon = True
        elif mm in ("0", "off", "false", "no"):
            want_multimon = False
        else:
            setting = self._eff("multimon", res, session)      # "on"/"off"/None
            want_multimon = (setting == "on") if setting else (self._n_monitors > 1)
        try:
            argv += build_display_args(path, extra, want_multimon)
        except (OSError, ValueError) as error:
            child._launching = False
            self._set_connecting(res["id"], res["title"], False, session)
            self._set_tile_state(res["id"], "● Failed", "state-ended", session)
            self._error(f"Could not prepare display settings: {error}")
            return
        backend = workspace_backend()
        workspace_title = None
        if res.get("type") == "Desktop" and self._eff("new_workspace", res, session) == "on" \
                and backend is not None and backend.available():
            workspace_title = "AVD-" + uuid.uuid4().hex
            # Last option wins over any custom /t flag. No resource/account data
            # enters the Shell bridge; the random title also works across PID namespaces.
            argv.append("/t:" + workspace_title)
        # Launch under a PTY. With FreeRDP built WITHOUT its own AAD webview,
        # /sec:aad prints "Browse to: <url>" and reads the redirect URL back
        # from stdin — but only when it thinks it's attached to a terminal, so
        # we give it a real pty. We service that prompt ourselves in a hidden
        # webview that shares the launcher's SSO cookies, making the
        # connection-time token silent. A new process group lets us tear down
        # the whole FreeRDP tree cleanly.
        with session.refresh_lock:
            if session.removed:
                child._launching = False
                self._set_connecting(res["id"], res["title"], False, session)
                return
            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen(argv, env=env, stdin=slave, stdout=slave,
                                        stderr=slave, start_new_session=True,
                                        close_fds=True)
            except Exception as error:
                os.close(master)
                child._launching = False
                self._set_connecting(res["id"], res["title"], False, session)
                self._set_tile_state(res["id"], "", None, session)
                self._error(f"Could not launch FreeRDP: {error}")
                return
            finally:
                os.close(slave)
        child._workspace_placement = backend.create_placement(proc, workspace_title) if workspace_title else None
        child._proc = proc
        child._ptym = master
        GLib.idle_add(self._tray_refresh)    # Focus/Disconnect now apply
        self._set_status(f"Launched {res['title']}", session)
        self._watch_session(child, res, proc, master, logpath, session)

    _ANSI = _re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

    def _watch_session(self, child, res, proc, master, logpath, session):
        """Read sdl-freerdp's PTY output: flip the tile to 'Connected' on logon,
        service each 'Browse to:' AAD prompt silently, and tidy up on exit."""
        state = {"connected": False}
        buf = b""
        try:
            log = open(logpath, "wb")
        except OSError:
            log = open(os.devnull, "wb")
        try:
            while True:
                try:
                    r, _, _ = select.select([master], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if r:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:            # EOF: child closed the pty (exited)
                        break
                    log.write(data)
                    log.flush()
                    buf += data
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = self._ANSI.sub(b"", raw).decode("utf-8", "replace").strip()
                        if line:
                            self._on_freerdp_line(child, res, master, line, state, session)
                elif proc.poll() is not None:
                    break
        finally:
            try:
                log.close()
            except Exception:
                pass
            try:
                os.close(master)
            except OSError:
                pass
        child._launching = False
        self._set_connecting(res["id"], res["title"], False, session)  # ensure overlay clears
        self._set_tile_state(res["id"], "○ Disconnected", "state-ended", session)
        self._set_status(f"{res['title']} session ended", session)
        GLib.timeout_add_seconds(
            6, lambda: (self._set_tile_state(res["id"], "", None, session), False)[1])

    def _on_freerdp_line(self, child, res, master, line, state, session):
        """Handle one line of FreeRDP output (called from the reader thread)."""
        if not state["connected"] and "Logon Info" in line:
            state["connected"] = True
            placement = getattr(child, "_workspace_placement", None)
            if placement:
                GLib.idle_add(placement.start)
            self._set_tile_state(res["id"], "● Connected", "state-connected", session)
            self._set_status(f"Connected to {res['title']}", session)
            self._set_connecting(res["id"], res["title"], False, session)  # desktop is up
        if line.startswith("Browse to: "):
            url = line[len("Browse to: "):].strip()
            if config.is_aad_login_url(url):
                # Resolve on the main thread (WebKit must run there).
                GLib.idle_add(self._resolve_aad, master, url, res, session)
            else:
                # Never load an arbitrary URL in the SSO-cookie WebView; a blank
                # line makes FreeRDP abort this auth instead of blocking on stdin.
                print(f"refusing non-Entra AAD URL: {url[:80]}", file=sys.stderr)
                try:
                    os.write(master, b"\n")
                except OSError:
                    pass

    def _resolve_aad(self, master, url, res, session):
        """Service FreeRDP's connection-time AAD prompt: load its authorize URL
        in a hidden webview sharing our SSO cookies, and write the resulting
        redirect URL back to FreeRDP's stdin. The window is shown only if it
        doesn't resolve silently within 3s (i.e. MFA/consent is really needed)."""
        if session.removed:
            try:
                os.write(master, b"\n")
            except OSError:
                pass
            return False
        wv = WebKit.WebView(network_session=self._web_session(session))
        self._apply_ua(wv)   # account-wide sign-in UA, must match sign-in (issue #28)
        # Unpresented window: a WebView loads/navigates while its window is
        # hidden (verified), so the silent path never flashes a window.
        win = Gtk.Window(title=f"Sign in — {res['title']}", transient_for=self.win)
        win.set_default_size(520, 640)
        win.set_child(wv)
        holder = {"done": False, "shown": False}

        def finish(redirect_uri):
            if holder["done"]:
                return
            holder["done"] = True
            try:
                os.write(master, (redirect_uri + "\n").encode())
            except OSError:
                pass
            win.destroy()

        def on_decide(view, decision, dtype):
            if dtype == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                uri = decision.get_navigation_action().get_request().get_uri()
                if "nativeclient" in uri and "code=" in uri:
                    decision.ignore()
                    finish(uri)
                    return True
            return False

        def on_destroy(*_):
            # Window closed before it resolved (user cancelled): send a blank
            # line so FreeRDP aborts this auth instead of blocking on stdin.
            if not holder["done"]:
                holder["done"] = True
                try:
                    os.write(master, b"\n")
                except OSError:
                    pass

        wv.connect("create", lambda *_a: None)     # block popups (no ghost windows)
        wv.connect("decide-policy", on_decide)
        win.connect("close-request", lambda w: (w.destroy(), True)[1])
        win.connect("destroy", on_destroy)
        wv.load_uri(url)

        def show_if_pending():
            if not holder["done"] and not holder["shown"]:
                holder["shown"] = True
                self._set_status(f"Signing in to {res['title']}…", session)
                win.present()
            return False
        GLib.timeout_add(3000, show_if_pending)
        return False

    # ---- tray icon ---------------------------------------------------------
    def _start_tray(self):
        if os.environ.get("AVD_TRAY", "").strip().lower() in ("0", "off", "false", "no"):
            return
        try:
            conn = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
        except GLib.Error:
            return
        # Monochrome "-symbolic" icon: tray hosts recolor it to match the bar.
        self._tray = TrayIcon(conn, APP_ID, APP_NAME, APP_ID + "-symbolic",
                              self._tray_icon_dir(),
                              on_activate=self._show_window,
                              on_action=self._on_tray_action,
                              on_availability=self._on_tray_availability)
        self._tray.start()
        self._tray_refresh()

    def _tray_icon_dir(self):
        """A host-visible directory holding the tray icon file itself. Hosts
        look IconName up in their own icon theme, which doesn't always include
        Flatpak's exported icons (Omarchy's bar doesn't), so point them at it.
        The Flatpak's tray/ dir has the icon both as <name>.svg and as <name>,
        since Quickshell loads <dir>/<IconName> without adding an extension."""
        try:
            info = GLib.KeyFile()
            info.load_from_file("/.flatpak-info", GLib.KeyFileFlags.NONE)
            return os.path.join(info.get_string("Instance", "app-path"),
                                "share/avd-feed-connect/tray")
        except GLib.Error:
            pass
        # Unpackaged run: the icon sits at the top of the source tree.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return root if os.path.exists(os.path.join(root, APP_ID + "-symbolic.svg")) else ""

    def _tray_refresh(self):
        if not self._tray:
            return False
        accounts, states = [], {}
        for session in self.accounts.values():
            resources = list(session.resources)
            known = {r['id'] for r in resources}
            # Keep established sessions accessible if a feed stops listing them.
            for rid, child in session.tiles.items():
                running = bool(child._proc and child._proc.poll() is None)
                state = self._sessions.get((session.id, rid)) or ("connecting" if child._launching else "idle")
                states[(session.id, rid)] = {'running': running, 'state': state}
                if running and rid not in known:
                    resources.append(child._res)
            accounts.append({'id': session.id, 'upn': session.display_name,
                             'resources': resources, 'removed': session.removed})
        self._tray.set_menu(tray_menu.build_accounts_menu(accounts, states))
        n = sum(1 for state in states.values() if state['running'])
        self._tray.set_tooltip(f"{n} session" + ("" if n == 1 else "s") + " connected" if n else "")
        return False

    def _show_window(self):
        if self.win:
            self.win.set_visible(True)
            self.win.present()

    def _set_tray_option(self, key, on):
        """Persist a tray checkbox (close-to-tray / start-in-tray)."""
        if on:
            self._settings[key] = True
        else:
            self._settings.pop(key, None)
        storage.save_settings(self._settings)

    def _on_close_request(self, win):
        # Opted in and a tray can bring it back: closing only hides the window
        # (sessions and the token refresh keep running). Otherwise close quits.
        if self._settings.get("_close_to_tray") and self._tray and self._tray.available:
            win.set_visible(False)
            return True
        return False

    def _start_hidden_timeout(self):
        # Started in the tray, but no tray host took the icon: show the window.
        self._start_hidden_source = 0
        self._show_window()
        return False

    def _on_tray_availability(self, available):
        for cb in self._tray_toggles:
            cb.set_sensitive(available)
            cb.set_tooltip_text(None if available else "Needs a system tray on your desktop")
        if available and self._start_hidden_source:
            # Started in the tray and the icon is up: stay hidden.
            GLib.source_remove(self._start_hidden_source)
            self._start_hidden_source = 0
        # The tray host went away (e.g. the bar restarted or was closed): never
        # leave the window hidden with no way back.
        if not available and self.win and not self.win.get_visible():
            self._show_window()

    def _on_tray_action(self, action):
        kind = action[0]
        session = self.accounts.get(action[1]) if len(action) > 2 else self.current
        rid = action[2] if len(action) > 2 else None
        if kind == "show":
            self._show_window()
        elif kind == "quit":
            self.quit()
        elif kind == "refresh":
            self._reload_feed()
        elif kind in ("connect", "connect-windowed"):
            if session is None or session.removed:
                return
            ch = self._child_for(rid, session)
            if ch is None:
                resource = next((r for r in session.resources if r['id'] == rid), None)
                if resource is not None:
                    ch = self._make_tile(resource, session)
                    session.tiles[rid] = ch
            if ch:
                self._on_tile_activated(self.grid, ch, windowed=kind == "connect-windowed")
        elif kind in ("focus", "disconnect"):
            ch = self._child_for(rid, session)
            proc = ch._proc if ch else None
            if not proc or proc.poll() is not None:
                return
            title = ch._res["title"]
            if kind == "focus":
                if not x11focus.focus_pid(proc.pid):
                    self._set_status(f"Couldn't bring {title} to the front")
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)   # own session (start_new_session)
                except (ProcessLookupError, PermissionError):
                    pass
                self._set_status(f"Disconnecting {title}…")

    # ---- per-resource settings -------------------------------------------
    def _eff(self, key, res, session=None):
        """Resolve a setting: per-resource value → global default → None(=auto)."""
        session = session or self.current
        rv = (session.resource_settings if session else {}).get(res["id"], {}).get(key, "auto")
        if rv and rv != "auto":
            return rv
        dv = self._settings.get("_default", {}).get(key, "auto")
        if dv and dv != "auto":
            return dv
        return None

    def _eff_ua(self):
        """Resolve the sign-in webview User-Agent, or "" for the device's own
        WebKit UA. It is a single account-wide value (the Default setting): every
        sign-in webview shares one persistent SSO cookie session, so they must
        all present the same identity or Entra refuses the silent token. A
        per-resource UA would only ever work when it equalled this, so there
        isn't one — this covers the account sign-in and every connection."""
        dv = self._settings.get("_default", {}).get("user_agent", "auto")
        return dv if dv and dv != "auto" else ""

    def _apply_ua(self, wv):
        """Spoof the sign-in webview's User-Agent when configured (issue #28)."""
        ua = self._eff_ua()
        if ua:
            try:
                wv.get_settings().set_user_agent(ua)
            except Exception:
                pass

    def _eff_extra(self, res, session):
        parts = [self._settings.get("_default", {}).get("extra_args", ""),
                 session.resource_settings.get(res["id"], {}).get("extra_args", ""),
                 os.environ.get("AVD_EXTRA_ARGS", "")]
        return " ".join(p.strip() for p in parts if p and p.strip()).strip()

    def _form_row(self, label, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_hexpand(True)
        widget.set_halign(Gtk.Align.END)
        row.append(lbl)
        row.append(widget)
        return row

    def _open_settings(self, res, session=None):
        session = session or self.current
        settings = session.resource_settings if res and session else self._settings
        key = res["id"] if res else "_default"
        cur = settings.get(key, {})
        title = f"Settings — {res['title']}" if res else "Default connection settings"
        auto_note = ("Automatic uses your Default settings, then the display."
                     if res else "Automatic matches your display and monitors.")

        dlg = Gtk.Window(title=title, transient_for=self.win, modal=True)
        dlg.add_css_class("avd-settings")
        dlg.set_default_size(440, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)

        scale_dd = Gtk.DropDown(model=Gtk.StringList.new(storage.SCALE_LABELS))
        sv = cur.get("scale", "auto")
        scale_dd.set_selected(storage.SCALE_VALUES.index(sv) if sv in storage.SCALE_VALUES else 0)
        box.append(self._form_row("Display scale", scale_dd))

        mm_dd = Gtk.DropDown(model=Gtk.StringList.new(storage.MULTIMON_LABELS))
        mv = cur.get("multimon", "auto")
        mm_dd.set_selected(storage.MULTIMON_VALUES.index(mv) if mv in storage.MULTIMON_VALUES else 0)
        box.append(self._form_row("Monitors", mm_dd))

        cert_dd = Gtk.DropDown(model=Gtk.StringList.new(storage.CERT_LABELS))
        cv = cur.get("cert", "auto")
        cert_dd.set_selected(storage.CERT_VALUES.index(cv) if cv in storage.CERT_VALUES else 0)
        box.append(self._form_row("Server certificate", cert_dd))

        hk_dd = Gtk.DropDown(model=Gtk.StringList.new(storage.HOTKEY_LABELS))
        hv = cur.get("hotkeys", "auto")
        hk_dd.set_selected(storage.HOTKEY_VALUES.index(hv) if hv in storage.HOTKEY_VALUES else 0)
        box.append(self._form_row("Client shortcuts", hk_dd))

        workspace_values = storage.WORKSPACE_VALUES if res else storage.WORKSPACE_DEFAULT_VALUES
        workspace_labels = storage.WORKSPACE_LABELS if res else storage.WORKSPACE_DEFAULT_LABELS
        workspace_dd = Gtk.DropDown(model=Gtk.StringList.new(workspace_labels))
        workspace_value = cur.get("new_workspace", "auto" if res else "off")
        workspace_dd.set_selected(workspace_values.index(workspace_value) if workspace_value in workspace_values else 0)
        workspace_dd.set_sensitive(res is None or res.get("type") == "Desktop")
        workspace_row = self._form_row("Start connection in a new virtual desktop", workspace_dd)
        workspace_supported = workspace_backend() is not None
        workspace_row.set_visible(workspace_supported)
        box.append(workspace_row)
        workspace_note = Gtk.Label(label="Desktop connections only. Requires the optional GNOME extension; switches to the new workspace when connected.", xalign=0, wrap=True)
        workspace_note.set_visible(workspace_supported)
        workspace_note.add_css_class("tile-sub")
        box.append(workspace_note)

        # Sign-in browser identity (User-Agent). Lets a tenant that blocks Linux
        # at sign-in via Conditional Access be satisfied by posing as Edge/Chrome
        # on Windows (issue #28). Stored value is the literal UA string; "Custom…"
        # reveals an entry. It is account-wide (Default only, not per resource):
        # every sign-in webview shares one SSO session, so a per-resource identity
        # that differed from the account's would just fail to connect.
        ua_dd = None
        if res is None:
            uav = cur.get("user_agent", "auto")
            ua_dd = Gtk.DropDown(model=Gtk.StringList.new(storage.UA_LABELS))
            ua_entry = Gtk.Entry()
            ua_entry.set_hexpand(True)
            ua_entry.set_placeholder_text("paste a User-Agent string")
            if uav in storage.UA_PRESETS:
                ua_dd.set_selected(storage.UA_PRESETS.index(uav))
            else:
                ua_dd.set_selected(len(storage.UA_LABELS) - 1)   # "Custom…"
                ua_entry.set_text(uav)
            box.append(self._form_row("Sign-in browser", ua_dd))
            ua_entry_row = self._form_row("Custom identity", ua_entry)
            ua_entry_row.set_visible(ua_dd.get_selected() == len(storage.UA_LABELS) - 1)
            box.append(ua_entry_row)
            ua_dd.connect(
                "notify::selected",
                lambda dd, *_: ua_entry_row.set_visible(
                    dd.get_selected() == len(storage.UA_LABELS) - 1))

        extra_entry = Gtk.Entry()
        extra_entry.set_text(cur.get("extra_args", ""))
        extra_entry.set_placeholder_text("advanced: e.g. /gfx /network:auto")
        extra_entry.set_hexpand(True)
        box.append(self._form_row("Advanced flags", extra_entry))

        note = Gtk.Label(label=auto_note + " Applies to your next connection.",
                         xalign=0, wrap=True)
        note.add_css_class("tile-sub")
        box.append(note)

        btnbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btnbox.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dlg.destroy())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")

        def do_save(*_):
            entry = {
                "scale": storage.SCALE_VALUES[scale_dd.get_selected()],
                "multimon": storage.MULTIMON_VALUES[mm_dd.get_selected()],
                "cert": storage.CERT_VALUES[cert_dd.get_selected()],
                "hotkeys": storage.HOTKEY_VALUES[hk_dd.get_selected()],
                "new_workspace": workspace_values[workspace_dd.get_selected()],
                "extra_args": extra_entry.get_text().strip(),
            }
            if ua_dd is not None:   # Default settings only; account-wide (issue #28)
                ua_sel = ua_dd.get_selected()
                if ua_sel == len(storage.UA_LABELS) - 1:        # "Custom…"
                    entry["user_agent"] = ua_entry.get_text().strip() or "auto"
                else:
                    entry["user_agent"] = storage.UA_PRESETS[ua_sel]
            # drop an all-default entry so the file stays tidy
            if entry["scale"] == "auto" and entry["multimon"] == "auto" \
                    and entry["cert"] == "auto" and entry["hotkeys"] == "auto" \
                    and entry["new_workspace"] == ("auto" if res else "off") \
                    and entry.get("user_agent", "auto") == "auto" \
                    and not entry["extra_args"]:
                settings.pop(key, None)
            else:
                settings[key] = entry
            if res:
                session.save_preferences()
            else:
                storage.save_settings(self._settings)
            dlg.destroy()
            self._set_status("Settings saved — applies to your next connection")
        save.connect("clicked", do_save)
        btnbox.append(cancel)
        btnbox.append(save)
        box.append(btnbox)
        dlg.set_child(box)
        dlg.present()

    def _error(self, msg):
        def show():
            self._show_window()
            d = Gtk.AlertDialog()
            d.set_modal(True)
            d.set_message(msg)
            d.show(self.win)
        GLib.idle_add(show)


if __name__ == "__main__":
    app = AvdApp()
    sys.exit(app.run(None))
