"""Command-line entry point — the feed/connect operations without the GUI.

Usage:
  avd-feed.py                 # sign in (once), list resources
  avd-feed.py list            # same
  avd-feed.py connect N       # download resource N's .rdp and launch it
  avd-feed.py rdp N           # just write resource N's .rdp, print the path
  avd-feed.py refresh         # prove silent token renewal, print expiry
  avd-feed.py logout          # forget the cached refresh token
Env overrides: AVD_TENANT (default "organizations"), AVD_UPN.
Add "devicecode" as a second arg to force the old device-code flow.
"""

import os
import sys

from . import config
from .accounts import ProfileRegistry, profile_dir
import shutil
from .feed import print_list


def _pick(resources, idx_arg):
    try:
        idx = int(idx_arg)
    except (TypeError, ValueError):
        sys.exit("give a resource index, e.g. avd-feed.py connect 0")
    if not 0 <= idx < len(resources):
        sys.exit(f"index {idx} out of range (0..{len(resources)-1})")
    return resources[idx]


def main():
    args = sys.argv[1:]
    dev = "devicecode" in args
    args = [a for a in args if a != "devicecode"]
    cmd = args[0] if args else "list"

    registry = ProfileRegistry()
    client = registry.client()
    if cmd == "logout":
        client.clear_token_cache()
        if registry.selected_id:
            directory = profile_dir(registry.root, registry.selected_id)
            registry.remove(registry.selected_id)
            shutil.rmtree(directory, ignore_errors=True)
        print("cached refresh token removed.")
        return

    if cmd == "refresh":
        client.get_token(verbose=True, use_device_code=dev)
        return

    token = client.get_token(use_device_code=dev)
    if cmd in ("list", "ls"):
        print_list(client.enumerate_feed(token))
    elif cmd in ("rdp", "connect", "conn"):
        res = _pick(client.enumerate_feed(token), args[1] if len(args) > 1 else None)
        path = client.download_rdp(token, res)
        print(f"wrote {path}")
        if cmd != "rdp":
            sys.exit(client.launch(path))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
