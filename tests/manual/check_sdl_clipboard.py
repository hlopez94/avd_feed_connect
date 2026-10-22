#!/usr/bin/env python3
"""Integration regression for SDL's X11 clipboard failure response.

Run on GNOME Wayland with wl-paste and the AVD Flatpak installed:
    python tests/manual/check_sdl_clipboard.py
Optional: --library path/to/libSDL3.so.0 tests a replacement SDL (absolute or relative).
WARNING: replaces the desktop clipboard with synthetic data.
No remote connection or Teams process is needed.
"""
import argparse
import ctypes as C
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

PLAIN = b"AVD synthetic clipboard test"
HTML = b"<p>AVD <b>synthetic</b> clipboard test</p>"


def provide(library, mode):
    sdl = C.CDLL(library)
    sdl.SDL_Init.argtypes = [C.c_uint]
    sdl.SDL_Init.restype = C.c_bool
    callback_type = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_char_p,
                               C.POINTER(C.c_size_t))
    sdl.SDL_SetClipboardData.argtypes = [callback_type, C.c_void_p, C.c_void_p,
                                       C.POINTER(C.c_char_p), C.c_size_t]
    sdl.SDL_SetClipboardData.restype = C.c_bool
    sdl.SDL_GetError.restype = C.c_char_p
    plain, html = C.create_string_buffer(PLAIN), C.create_string_buffer(HTML)

    @callback_type
    def data(_userdata, mime, size):
        if mime == b"text/html":
            if mode == "failure":
                size[0] = 0
                return None
            size[0] = len(HTML) if mode == "html" else 0
            return C.addressof(html)
        size[0] = len(PLAIN)
        return C.addressof(plain)

    if not sdl.SDL_Init(0x20):
        raise RuntimeError(sdl.SDL_GetError())
    try:
        mimes = (C.c_char_p * 2)(b"text/plain;charset=utf-8", b"text/html")
        if not sdl.SDL_SetClipboardData(data, None, None, mimes, 2):
            raise RuntimeError(sdl.SDL_GetError())
        print("READY", flush=True)
        while True:
            sdl.SDL_PumpEvents()
            time.sleep(.005)
    finally:
        sdl.SDL_Quit()


def check(args):
    script = str(Path(__file__).resolve())
    library = str(Path(args.library).resolve()) if args.library else None
    for mode in ("failure", "html", "empty"):
        instance_file = tempfile.TemporaryFile()
        command = ["flatpak", "run", f"--filesystem={script}:ro",
                   f"--instance-id-fd={instance_file.fileno()}",
                   "--env=SDL_VIDEODRIVER=x11"]
        if library:
            command.append(f"--filesystem={Path(library).parent}:ro")
        command += ["--command=python3", args.flatpak, script, "--provide", mode,
                    "--library", library or "/app/lib/libSDL3.so.0"]
        provider = subprocess.Popen(command, stdout=subprocess.PIPE, text=True,
                                    pass_fds=(instance_file.fileno(),))
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(provider.stdout, selectors.EVENT_READ)
                if not selector.select(10) or provider.stdout.readline().strip() != "READY":
                    raise RuntimeError("SDL provider did not start")
            # Allow the compositor to receive the new TARGETS announcement.
            deadline = time.monotonic() + 3
            while True:
                types = subprocess.run(["wl-paste", "--list-types"],
                                       capture_output=True, timeout=3)
                if b"text/html" in types.stdout:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("HTML not advertised")
                time.sleep(.05)
            for mime, expected in (("text/plain;charset=utf-8", PLAIN),
                                   ("text/html", HTML if mode == "html" else b"")):
                start = time.monotonic()
                try:
                    result = subprocess.run(["wl-paste", "--no-newline", "--type", mime],
                                            capture_output=True, timeout=3)
                except subprocess.TimeoutExpired as error:
                    raise AssertionError(f"{mode}: {mime} transfer never completed") from error
                assert result.returncode == 0, result.stderr.decode(errors="replace")
                assert result.stdout == expected, f"{mode}: unexpected data for {mime}"
                print(f"PASS {mode}: {mime}, {len(result.stdout)} bytes, "
                      f"{time.monotonic() - start:.3f}s", flush=True)
        finally:
            instance_file.seek(0)
            instance = instance_file.read().decode().strip()
            try:
                if instance:
                    subprocess.run(["flatpak", "kill", instance], capture_output=True, timeout=5)
            finally:
                try:
                    provider.terminate()
                    try:
                        provider.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        provider.kill()
                        provider.wait()
                finally:
                    provider.stdout.close()
                    instance_file.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flatpak", default="io.github.shakeelosmani.avd_feed_connect")
    parser.add_argument("--library")
    parser.add_argument("--provide", choices=("failure", "html", "empty"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.provide:
        provide(args.library, args.provide)
    else:
        if not os.environ.get("WAYLAND_DISPLAY"):
            parser.error("Run this integration check in a Wayland desktop session")
        check(args)
