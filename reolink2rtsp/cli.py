"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

from . import __version__
from .baichuan import BaichuanClient, STREAM_MAIN, STREAM_SUB, STREAM_EXTERN
from .bcmedia import BcMediaParser, StreamInfo, VideoFrame
from .config import ConfigError, load, parse_users
from .h26x import ParameterSets, split_nals
from .rtsp import RtspServer
from .source import CameraSource

_LOG = logging.getLogger("reolink2rtsp")

DEFAULT_CONFIG = "reolink2rtsp.ini"


def _setup_logging(level):
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


async def _serve(config):
    sources = {}
    servers = []

    for port, cameras in sorted(config.by_port().items()):
        port_sources = {}
        for camera in cameras:
            # The bare path serves the configured stream; each of the camera's
            # other encodes gets its own sub-path. Sources are lazy, so the
            # extra paths cost nothing until a client actually asks for one.
            source = CameraSource(camera)
            sources[camera.name] = source
            port_sources[camera.url_path] = source
            port_sources["{}/{}".format(camera.url_path, camera.stream)] = source

            for stream in camera.extra_streams:
                key = "{}/{}".format(camera.url_path, stream)
                if key in port_sources:
                    continue
                extra = CameraSource(camera.for_stream(stream))
                sources[extra.name] = extra
                port_sources[key] = extra
        users = cameras[0].users
        servers.append(
            RtspServer(
                port_sources,
                bind=config.bind,
                port=port,
                mtu=config.mtu,
                users=users,
                describe_timeout=config.describe_timeout,
            )
        )

    if not servers:
        _LOG.error("no enabled cameras in the config")
        return 1

    for server in servers:
        await server.start()

    _LOG.info(
        "reolink2rtsp %s ready - %d camera(s) on %d port(s)",
        __version__,
        len(sources),
        len(servers),
    )

    for camera in config.cameras:
        if camera.enabled and camera.always_on:
            sources[camera.name]._ensure_running()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: fall back to the default KeyboardInterrupt path.
            pass

    serve_tasks = [asyncio.ensure_future(s.serve_forever()) for s in servers]
    stop_task = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait(
            serve_tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED
        )
    except KeyboardInterrupt:
        pass
    finally:
        _LOG.info("shutting down")
        for task in serve_tasks + [stop_task]:
            task.cancel()
        for server in servers:
            await server.stop()
    return 0


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


async def _probe(host, username, password, port, stream, channel, seconds):
    print("connecting to {}:{} ...".format(host, port))
    client = BaichuanClient(host, username, password, port=port)
    stream_num = None
    try:
        started = time.monotonic()
        await client.login()
        print("login OK in {:.2f}s".format(time.monotonic() - started))
        if client.device_info:
            print("\ndevice info:")
            for key in sorted(client.device_info):
                print("  {:<18} {}".format(key, client.device_info[key]))

        print("\nrequesting {} stream on channel {} ...".format(stream, channel))
        stream_num, queue = await client.start_video(stream, channel)

        parser = BcMediaParser()
        params = None
        codec = None
        keyframes = pframes = 0
        total = 0
        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    queue.get(), max(0.5, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                break
            if chunk is None:
                print("stream ended early")
                break
            total += len(chunk)
            for frame in parser.feed(chunk):
                if isinstance(frame, StreamInfo):
                    print("stream info: {}x{} @ {} fps".format(
                        frame.width, frame.height, frame.fps))
                elif isinstance(frame, VideoFrame):
                    if codec is None:
                        codec = frame.codec
                        params = ParameterSets(codec)
                        print("codec: {}".format(codec))
                    for nal in split_nals(frame.data):
                        params.observe(nal)
                    if frame.keyframe:
                        keyframes += 1
                    else:
                        pframes += 1

        elapsed = seconds
        print("\n--- {:.0f}s summary ---".format(elapsed))
        print("  bytes received : {:,}  ({:.0f} kbit/s)".format(
            total, total * 8 / 1000.0 / max(elapsed, 0.001)))
        print("  key frames     : {}".format(keyframes))
        print("  delta frames   : {}".format(pframes))
        print("  frame rate     : {:.1f} fps".format((keyframes + pframes) / max(elapsed, 0.001)))
        if params is not None:
            print("  parameter sets : {}".format("complete" if params.ready else "INCOMPLETE"))
            resolution = params.resolution()
            if resolution:
                print("  resolution     : {}x{}".format(*resolution))
        if keyframes == 0:
            print("\n  no key frames seen - the stream will not be decodable")
            return 1
        print("\nprobe OK - this camera can be served over RTSP")
        return 0
    finally:
        if stream_num is not None:
            try:
                await client.stop_video(stream_num, stream, channel)
            except Exception:
                pass
        await client.close()


# --------------------------------------------------------------------------- #


_STREAM_XML = {"main": "mainStream", "sub": "subStream", "extern": "thirdStream"}


async def _encoder(host, username, password, port, channel,
                   stream, bitrate, framerate, gop):
    """Show, and optionally change, the camera's own encoder settings."""
    client = BaichuanClient(host, username, password, port=port)
    try:
        await client.login()
        _, enc = await client.get_enc(channel)
        if not enc:
            print("camera returned no encoder settings")
            return 1

        print("{}  channel {}\n".format(host, channel))
        print("  {:<13} {:<12} {:>5} {:>9} {:>5}  {}".format(
            "stream", "resolution", "fps", "kbit/s", "gop", "profile"))
        for name in ("mainStream", "thirdStream", "subStream"):
            settings = enc.get(name)
            if not settings:
                continue
            print("  {:<13} {:<12} {:>5} {:>9} {:>5}  {}".format(
                name,
                "{}x{}".format(settings.get("width", "?"), settings.get("height", "?")),
                settings.get("frame", "?"), settings.get("bitRate", "?"),
                settings.get("gop", "-"), settings.get("encoderProfile", "?")))
        print("\n  reolink2rtsp calls these main / extern / sub respectively")

        if bitrate is None and framerate is None and gop is None:
            return 0

        print()
        try:
            changed = await client.set_enc(
                channel, stream, bitrate=bitrate, framerate=framerate, gop=gop)
        except Exception as exc:
            print("failed: {}".format(exc))
            return 1
        if not changed:
            print("already set as requested, nothing to do")
            return 0
        for key, (old, new) in sorted(changed.items()):
            print("  {} {} -> {}".format(key, old, new))

        _, after = await client.get_enc(channel)
        settings = after.get(_STREAM_XML.get(stream, stream), {})
        print("\nreadback: {}x{}  {} fps  {} kbit/s".format(
            settings.get("width"), settings.get("height"),
            settings.get("frame"), settings.get("bitRate")))
        return 0
    finally:
        await client.close()


def _apply_camera_flags(config, args):
    """Apply the command-line shorthands that target every camera."""
    only = set(args.only or ())
    if only:
        known = {camera.name for camera in config.cameras}
        unknown = only - known
        if unknown:
            raise ConfigError(
                "--only names no such camera: {} (known: {})".format(
                    ", ".join(sorted(unknown)), ", ".join(sorted(known))
                )
            )

    users = parse_users(args.users) if args.users else None

    for camera in config.cameras:
        if only:
            camera.enabled = camera.name in only
        if args.username:
            camera.username = args.username
        if args.password:
            camera.password = args.password
        if args.stream:
            camera.stream = args.stream
        if users:
            camera.users = dict(users)
        if args.always_on:
            camera.always_on = True
        if args.audio is not None:
            camera.audio = args.audio
        if args.bitrate is not None:
            camera.bitrate = args.bitrate
        if args.framerate is not None:
            camera.framerate = args.framerate
        if args.gop is not None:
            camera.gop = args.gop

    if not any(camera.enabled for camera in config.cameras):
        raise ConfigError("every camera is disabled - nothing to serve")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reolink2rtsp",
        description="Serve RTSP from Reolink cameras that only speak the "
                    "proprietary Baichuan protocol on TCP 9000.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-l", "--log-level", default=None,
        help="DEBUG, INFO, WARNING, ERROR (overrides the config file)",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the RTSP server (default)")
    serve.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, help="config file path"
    )
    serve.add_argument(
        "-o", "--set", dest="overrides", action="append", metavar="SECTION.KEY=VALUE",
        help="override any config value; repeatable. A bare KEY=VALUE targets "
             "[server]. Sections are created on demand, so cameras can be "
             "defined entirely from the command line, e.g. "
             "-o camera:test.host=192.168.15.60 -o camera:test.rtsp_port=8554",
    )
    # Convenience shorthands for the [server] section.
    serve.add_argument("--bind", help="address to listen on")
    serve.add_argument("--base-port", type=int, help="first RTSP port to allocate")
    serve.add_argument("--mtu", type=int, help="RTP payload MTU")
    serve.add_argument(
        "--describe-timeout", type=float,
        help="seconds DESCRIBE waits for the first key frame",
    )
    # Convenience shorthands applied to every camera.
    serve.add_argument("--username", help="camera username for all cameras")
    serve.add_argument("--password", help="camera password for all cameras")
    serve.add_argument(
        "--stream", choices=[STREAM_MAIN, STREAM_SUB, STREAM_EXTERN],
        help="stream to pull from all cameras",
    )
    serve.add_argument(
        "--users", help="RTSP logins for all cameras, e.g. 'test:test, ops:pw'"
    )
    serve.add_argument(
        "--only", metavar="NAME", action="append",
        help="serve only these cameras; repeatable",
    )
    serve.add_argument(
        "--always-on", action="store_true",
        help="keep cameras connected even with no viewers",
    )
    serve.add_argument("--bitrate", type=int,
                       help="set the camera's encoder bitrate (kbps) for the served stream")
    serve.add_argument("--framerate", type=int,
                       help="set the camera's encoder frame rate for the served stream")
    serve.add_argument("--gop", type=int, help="set the camera's key-frame interval")
    audio = serve.add_mutually_exclusive_group()
    audio.add_argument(
        "--audio", dest="audio", action="store_true", default=None,
        help="serve the camera's AAC audio as a second RTSP track (default)",
    )
    audio.add_argument(
        "--no-audio", dest="audio", action="store_false",
        help="video only",
    )

    probe = sub.add_parser(
        "probe", help="connect to one camera and report what it streams"
    )
    probe.add_argument("host")
    probe.add_argument("-u", "--username", default="admin")
    probe.add_argument(
        "-p", "--password", default=os.environ.get("REOLINK_PASSWORD", "")
    )
    probe.add_argument("--port", type=int, default=9000)
    probe.add_argument(
        "-s", "--stream", default=STREAM_MAIN,
        choices=[STREAM_MAIN, STREAM_SUB, STREAM_EXTERN],
    )
    probe.add_argument("--channel", type=int, default=0)
    probe.add_argument(
        "-t", "--seconds", type=float, default=8.0,
        help="how long to sample the stream",
    )

    encoder = sub.add_parser(
        "encoder",
        help="show, or change, the camera's own encoder settings "
             "(the only way to alter bandwidth without transcoding)",
    )
    encoder.add_argument("host")
    encoder.add_argument("-u", "--username", default="admin")
    encoder.add_argument(
        "-p", "--password", default=os.environ.get("REOLINK_PASSWORD", ""))
    encoder.add_argument("--port", type=int, default=9000)
    encoder.add_argument("--channel", type=int, default=0)
    encoder.add_argument(
        "-s", "--stream", default=STREAM_MAIN,
        choices=[STREAM_MAIN, STREAM_SUB, STREAM_EXTERN],
        help="which stream to modify",
    )
    encoder.add_argument("--bitrate", type=int, help="kbit/s")
    encoder.add_argument("--framerate", type=int, help="frames per second")
    encoder.add_argument("--gop", type=int, help="key-frame interval")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to `serve` when no subcommand is given.
    commands = ("serve", "probe", "encoder")
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        if not any(a in commands for a in argv):
            argv.insert(0, "serve")
    elif argv[0] not in commands + ("-h", "--help", "--version"):
        argv.insert(0, "serve")

    args = build_parser().parse_args(argv)

    if args.command == "encoder":
        _setup_logging(args.log_level or "WARNING")
        if not args.password:
            print("no password given: pass --password or set REOLINK_PASSWORD",
                  file=sys.stderr)
            return 2
        return asyncio.run(_encoder(
            args.host, args.username, args.password, args.port, args.channel,
            args.stream, args.bitrate, args.framerate, args.gop))

    if args.command == "probe":
        _setup_logging(args.log_level or "INFO")
        if not args.password:
            print(
                "no password given: pass --password or set REOLINK_PASSWORD",
                file=sys.stderr,
            )
            return 2
        return asyncio.run(
            _probe(
                args.host, args.username, args.password, args.port,
                args.stream, args.channel, args.seconds,
            )
        )

    overrides = list(args.overrides or [])
    for flag, key in (
        ("bind", "bind"),
        ("base_port", "base_port"),
        ("mtu", "mtu"),
        ("describe_timeout", "describe_timeout"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            overrides.append("server.{}={}".format(key, value))

    try:
        config = load(args.config, overrides)
        _apply_camera_flags(config, args)
    except ConfigError as exc:
        _setup_logging(args.log_level or "INFO")
        _LOG.error("%s", exc)
        return 2

    _setup_logging(args.log_level or config.log_level)
    try:
        return asyncio.run(_serve(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
