"""Download a resource's ready-to-use .rdp file from its feed ResourceFile URL."""

import hashlib
import os
import re
import sys

from .. import config, http
from ..i18n import _ as tr


def download_rdp(token, res, output_dir=None):
    """Fetch resource ``res``'s .rdp text and write it under the feed data dir,
    returning the path. ``res`` is a resource dict from the feed."""
    if not res["rdp_url"]:
        sys.exit(tr("Resource {resource} has no ResourceFile URL").format(resource=repr(res["title"])))
    st, _, rdp = http.get(res["rdp_url"], token)
    if st != 200:
        sys.exit(tr("RDP download failed ({status}): {details}").format(status=st, details=rdp[:300]))
    directory = output_dir or config.OUT
    os.makedirs(directory, mode=0o700, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", res["title"])[:80]
    if output_dir:
        safe += "_" + hashlib.sha256(res["id"].encode()).hexdigest()[:16]
    path = os.path.join(directory, safe + ".rdp")
    with open(path, "w") as f:
        f.write(rdp)
    return path


def set_rdp_dynamic_resolution(path):
    """Remove feed settings that conflict with a dynamically resized window."""
    with open(path) as source:
        lines = source.read().splitlines()
    properties = {"smart sizing": "0", "screen mode id": "1"}
    lines = [line for line in lines
             if line.partition(":")[0].strip().lower() not in properties]
    lines.extend(f"{name}:i:{value}" for name, value in properties.items())
    with open(path, "w") as destination:
        destination.write("\n".join(lines) + "\n")


def set_rdp_multimon(path, enabled):
    """Force the .rdp's ``use multimon`` property to match the chosen setting.

    AVD feed .rdp files ship ``use multimon:i:1`` and that connection-file
    property overrides sdl-freerdp's ``-multimon`` / ``/multimon`` CLI flag, so
    the in-app Single/All-monitors choice is only honored by rewriting the file
    itself (the CLI flag alone is ignored). Rewrites in place; safe to no-op on
    any I/O error.
    """
    val = "1" if enabled else "0"
    try:
        with open(path) as f:
            lines = f.read().splitlines()
        out, found = [], False
        for ln in lines:
            if ln.lower().startswith("use multimon:i:"):
                out.append(f"use multimon:i:{val}")
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"use multimon:i:{val}")
        with open(path, "w") as f:
            f.write("\n".join(out) + "\n")
    except OSError:
        pass
