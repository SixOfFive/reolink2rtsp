"""Configuration loading.

INI format, parsed with the standard library only. Each ``[camera:<name>]``
section describes one camera and the RTSP endpoint it is published on, so every
camera can have its own port and its own set of RTSP credentials.

Any value may reference an environment variable as ``${VAR}`` or ``${VAR:-default}``,
which keeps real camera passwords out of a file you might commit.
"""

from __future__ import annotations

import configparser
import os
import re
import shlex

__all__ = ["Config", "CameraConfig", "load", "apply_overrides",
           "parse_users", "ConfigError"]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_BASE_PORT = 554
DEFAULT_BC_PORT = 9000

_TRUE = ("1", "true", "yes", "on")


class ConfigError(Exception):
    pass


def _expand(value):
    """Replace ${VAR} / ${VAR:-default} with the environment."""
    if value is None:
        return None

    def replace(match):
        name, default = match.group(1), match.group(2)
        found = os.environ.get(name)
        if found is not None:
            return found
        if default is not None:
            return default
        raise ConfigError(
            "environment variable {} is referenced in the config but not set".format(name)
        )

    return _ENV_PATTERN.sub(replace, value)


def parse_users(raw):
    """Parse ``user:pass, other:pass`` (or whitespace separated) into a dict."""
    users = {}
    if not raw:
        return users
    for item in re.split(r"[,\n]", raw):
        item = item.strip()
        if not item:
            continue
        # shlex keeps quoted passwords containing spaces intact
        for token in shlex.split(item):
            if ":" not in token:
                raise ConfigError(
                    "RTSP user entry {!r} must be in user:password form".format(token)
                )
            user, password = token.split(":", 1)
            users[user] = password
    return users


class CameraConfig(object):
    def __init__(self, name, **kwargs):
        self.name = name
        self.host = kwargs["host"]
        self.port = kwargs.get("port", DEFAULT_BC_PORT)
        self.username = kwargs.get("username", "admin")
        self.password = kwargs.get("password", "")
        self.channel = kwargs.get("channel", 0)
        self.stream = kwargs.get("stream", "main")
        self.enabled = kwargs.get("enabled", True)

        # RTSP endpoint
        self.rtsp_port = kwargs["rtsp_port"]
        self.rtsp_path = kwargs.get("rtsp_path") or name
        self.users = kwargs.get("users") or {}

        # Behaviour
        self.timeout = kwargs.get("timeout", 15.0)
        self.ping_interval = kwargs.get("ping_interval", 20.0)
        self.idle_timeout = kwargs.get("idle_timeout", 30.0)
        self.always_on = kwargs.get("always_on", False)
        self.queue_size = kwargs.get("queue_size", 120)

    @property
    def url_path(self):
        return self.rtsp_path.strip("/")

    def describe(self, bind):
        host = bind if bind not in ("0.0.0.0", "::") else "<host>"
        auth = ""
        if self.users:
            user = sorted(self.users)[0]
            auth = "{}:{}@".format(user, self.users[user])
        return "rtsp://{}{}:{}/{}".format(auth, host, self.rtsp_port, self.url_path)


class Config(object):
    def __init__(self, bind="0.0.0.0", mtu=1400, log_level="INFO", cameras=None,
                 describe_timeout=20.0, status_port=0):
        self.bind = bind
        self.mtu = mtu
        self.log_level = log_level
        self.cameras = cameras or []
        self.describe_timeout = describe_timeout
        self.status_port = status_port

    def by_port(self):
        """Group enabled cameras by the RTSP port they are served on."""
        grouped = {}
        for camera in self.cameras:
            if camera.enabled:
                grouped.setdefault(camera.rtsp_port, []).append(camera)
        return grouped


def _getint(section, key, default):
    raw = _expand(section.get(key))
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("{!r} must be an integer, got {!r}".format(key, raw))


def _getfloat(section, key, default):
    raw = _expand(section.get(key))
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError("{!r} must be a number, got {!r}".format(key, raw))


def _getbool(section, key, default):
    raw = _expand(section.get(key))
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


def apply_overrides(parser, overrides):
    """Apply ``SECTION.KEY=VALUE`` strings on top of a parsed config.

    A bare ``KEY=VALUE`` targets ``[server]``. Sections are created on demand,
    so a whole camera can be defined from the command line alone::

        -o camera:test.host=192.168.15.60 -o camera:test.rtsp_port=8554
    """
    for item in overrides or ():
        if "=" not in item:
            raise ConfigError(
                "override {!r} must be SECTION.KEY=VALUE".format(item)
            )
        target, value = item.split("=", 1)
        target = target.strip()
        if "." in target:
            section, key = target.split(".", 1)
        else:
            section, key = "server", target
        section, key = section.strip(), key.strip()
        if not section or not key:
            raise ConfigError("override {!r} must be SECTION.KEY=VALUE".format(item))
        if not parser.has_section(section):
            parser.add_section(section)
        parser.set(section, key, value)


def load(path, overrides=None):
    """Load and validate a configuration file, applying any CLI overrides.

    *path* may be None or missing when overrides alone define the cameras.
    """
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # keep key case as written

    if path:
        if not os.path.exists(path):
            if not overrides:
                raise ConfigError("config file not found: {}".format(path))
        else:
            with open(path, "r", encoding="utf8") as handle:
                parser.read_file(handle)

    apply_overrides(parser, overrides)

    server = parser["server"] if parser.has_section("server") else {}
    bind = _expand(server.get("bind", "0.0.0.0"))
    mtu = _getint(server, "mtu", 1400)
    log_level = _expand(server.get("log_level", "INFO")).upper()
    base_port = _getint(server, "base_port", DEFAULT_BASE_PORT)
    describe_timeout = _getfloat(server, "describe_timeout", 20.0)
    status_port = _getint(server, "status_port", 0)

    defaults = parser["defaults"] if parser.has_section("defaults") else {}
    default_users = parse_users(_expand(defaults.get("users", "")))

    cameras = []
    used_ports = set()
    auto_port = base_port

    for section_name in parser.sections():
        if not section_name.startswith("camera:"):
            continue
        name = section_name.split(":", 1)[1].strip()
        if not name:
            raise ConfigError("camera section {!r} has no name".format(section_name))
        section = parser[section_name]

        host = _expand(section.get("host"))
        if not host:
            raise ConfigError("camera {!r} is missing 'host'".format(name))

        rtsp_port = _getint(section, "rtsp_port", 0)
        if rtsp_port == 0:
            while auto_port in used_ports:
                auto_port += 1
            rtsp_port = auto_port
            auto_port += 1
        if rtsp_port in used_ports:
            # Sharing a port is allowed as long as the paths differ; validated below.
            pass
        used_ports.add(rtsp_port)

        users = parse_users(_expand(section.get("users", ""))) or dict(default_users)

        stream = _expand(section.get("stream", "main")).lower()
        if stream not in ("main", "sub", "extern"):
            raise ConfigError(
                "camera {!r}: stream must be main, sub or extern (got {!r})".format(
                    name, stream
                )
            )

        cameras.append(
            CameraConfig(
                name,
                host=host,
                port=_getint(section, "port", DEFAULT_BC_PORT),
                username=_expand(section.get("username", "admin")),
                password=_expand(section.get("password", "")),
                channel=_getint(section, "channel", 0),
                stream=stream,
                enabled=_getbool(section, "enabled", True),
                rtsp_port=rtsp_port,
                rtsp_path=_expand(section.get("rtsp_path", "")) or name,
                users=users,
                timeout=_getfloat(section, "timeout", 15.0),
                ping_interval=_getfloat(section, "ping_interval", 20.0),
                idle_timeout=_getfloat(section, "idle_timeout", 30.0),
                always_on=_getbool(section, "always_on", False),
                queue_size=_getint(section, "queue_size", 120),
            )
        )

    if not cameras:
        raise ConfigError("no [camera:<name>] sections found in {}".format(path))

    # Two cameras may share a port only if their paths differ.
    seen = {}
    for camera in cameras:
        key = (camera.rtsp_port, camera.url_path)
        if key in seen:
            raise ConfigError(
                "cameras {!r} and {!r} both serve port {} path /{}".format(
                    seen[key], camera.name, camera.rtsp_port, camera.url_path
                )
            )
        seen[key] = camera.name

    # Every camera on a shared port must agree on credentials, since auth is
    # negotiated per connection before a path is known.
    grouped = {}
    for camera in cameras:
        grouped.setdefault(camera.rtsp_port, []).append(camera)
    for port, group in grouped.items():
        first = group[0]
        for other in group[1:]:
            if other.users != first.users:
                raise ConfigError(
                    "cameras {!r} and {!r} share port {} but define different "
                    "RTSP users; give them separate ports or identical users".format(
                        first.name, other.name, port
                    )
                )

    return Config(
        bind=bind,
        mtu=mtu,
        log_level=log_level,
        cameras=cameras,
        describe_timeout=describe_timeout,
        status_port=status_port,
    )
