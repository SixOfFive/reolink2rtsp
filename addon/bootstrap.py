#!/usr/bin/env python3
"""Turn Home Assistant add-on options into a reolink2rtsp config, then serve.

The Supervisor writes the user's options to /data/options.json. Rather than
wrangle that into an INI with shell and jq, this reads it directly, writes the
config, and hands over to the normal entry point.
"""

from __future__ import annotations

import json
import os
import sys

OPTIONS = os.environ.get("ADDON_OPTIONS", "/data/options.json")
CONFIG = os.environ.get("ADDON_CONFIG", "/data/reolink2rtsp.ini")

# Options that belong to a camera section, and how to render them.
_CAMERA_KEYS = (
    "host", "username", "password", "stream", "rtsp_port", "rtsp_path",
    "bitrate", "framerate", "gop", "channel", "port", "extra_streams",
    "timeout", "ping_interval", "idle_timeout", "queue_size",
)
_CAMERA_FLAGS = ("audio", "always_on", "enabled")

# Add-on log levels are broader than Python's; map the extras onto real ones.
_LOG_LEVELS = {
    "trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "notice": "INFO",
    "warning": "WARNING", "error": "ERROR", "fatal": "CRITICAL",
}


def _fail(message):
    print("[reolink2rtsp] {}".format(message), file=sys.stderr, flush=True)
    raise SystemExit(1)


def load_options():
    if not os.path.exists(OPTIONS):
        _fail("{} not found - is this running as a Home Assistant add-on?".format(OPTIONS))
    with open(OPTIONS, "r", encoding="utf8") as handle:
        try:
            return json.load(handle)
        except ValueError as exc:
            _fail("could not parse {}: {}".format(OPTIONS, exc))


def build_config(options):
    lines = [
        "; Generated from the add-on options - edit those in Home Assistant,",
        "; not this file. It is rewritten on every start.",
        "",
        "[server]",
        "bind = 0.0.0.0",
    ]
    for key in ("base_port", "mtu", "describe_timeout"):
        if options.get(key) not in (None, ""):
            lines.append("{} = {}".format(key, options[key]))
    lines.append("log_level = {}".format(
        _LOG_LEVELS.get(str(options.get("log_level", "info")).lower(), "INFO")))
    lines.append("")

    default_users = str(options.get("rtsp_users") or "").strip()
    if default_users:
        lines += ["[defaults]", "users = {}".format(default_users), ""]

    cameras = options.get("cameras") or []
    if not cameras:
        _fail("no cameras configured - add at least one in the add-on options")

    enabled = 0
    for camera in cameras:
        name = str(camera.get("name") or "").strip()
        if not name:
            _fail("a camera entry has no name")
        if not str(camera.get("host") or "").strip():
            _fail("camera {!r} has no host".format(name))

        is_enabled = camera.get("enabled", True)
        if is_enabled:
            if not str(camera.get("password") or ""):
                _fail(
                    "camera {!r} has no password. Set it in the add-on "
                    "configuration, or set enabled: false to skip it.".format(name)
                )
            enabled += 1

        lines.append("[camera:{}]".format(name))
        for key in _CAMERA_KEYS:
            value = camera.get(key)
            if value in (None, ""):
                continue
            lines.append("{} = {}".format(key, value))
        for key in _CAMERA_FLAGS:
            if key in camera:
                lines.append("{} = {}".format(key, "true" if camera[key] else "false"))
        # A camera without its own logins inherits [defaults].
        if str(camera.get("rtsp_users") or "").strip():
            lines.append("users = {}".format(camera["rtsp_users"].strip()))
        lines.append("")

    if not enabled:
        _fail("every camera is disabled - nothing to serve")

    return "\n".join(lines) + "\n", enabled


def main():
    options = load_options()
    text, enabled = build_config(options)

    with open(CONFIG, "w", encoding="utf8") as handle:
        handle.write(text)

    print("[reolink2rtsp] {} camera(s) enabled, config written to {}".format(
        enabled, CONFIG), flush=True)

    sys.path.insert(0, "/opt")
    from reolink2rtsp.cli import main as serve  # noqa: E402

    raise SystemExit(serve(["serve", "-c", CONFIG]))


if __name__ == "__main__":
    main()
