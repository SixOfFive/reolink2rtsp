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
from .config import ConfigError, load
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
            source = CameraSource(camera)
            sources[camera.name] = source
            port_sources[camera.url_path] = source
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
    loop = asyncio.get_event_loop()
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
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to `serve` when no subcommand is given.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        if not any(a in ("serve", "probe") for a in argv):
            argv.insert(0, "serve")
    elif argv[0] not in ("serve", "probe", "-h", "--help", "--version"):
        argv.insert(0, "serve")

    args = build_parser().parse_args(argv)

    if args.command == "probe":
        _setup_logging(args.log_level or "INFO")
        if not args.password:
            print(
                "no password given: pass --password or set REOLINK_PASSWORD",
                file=sys.stderr,
            )
            return 2
        return asyncio.get_event_loop().run_until_complete(
            _probe(
                args.host, args.username, args.password, args.port,
                args.stream, args.channel, args.seconds,
            )
        )

    try:
        config = load(args.config)
    except ConfigError as exc:
        _setup_logging(args.log_level or "INFO")
        _LOG.error("%s", exc)
        return 2

    _setup_logging(args.log_level or config.log_level)
    try:
        return asyncio.get_event_loop().run_until_complete(_serve(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
