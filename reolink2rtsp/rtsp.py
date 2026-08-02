"""A small RTSP 1.0 server (RFC 2326) that serves the camera sources.

Supports both transports clients actually use:

* ``RTP/AVP/TCP`` - RTP interleaved into the RTSP control connection. This is
  what Frigate/go2rtc pick by default and what survives NAT and firewalls.
* ``RTP/AVP`` - classic RTP/RTCP over UDP.

Media is passed through untouched, so there is no transcoding anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import socket
import struct
import time
from urllib.parse import unquote, urlparse

from .h26x import H265
from .rtp import RtpPacketizer

_LOG = logging.getLogger(__name__)

SERVER_NAME = "reolink2rtsp"
RTSP_VERSION = "RTSP/1.0"
PAYLOAD_TYPE = 96
SESSION_TIMEOUT = 60

_STATUS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    454: "Session Not Found",
    455: "Method Not Valid In This State",
    461: "Unsupported Transport",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


# Matches key="value" or key=value inside an Authorization header.
_DIGEST_FIELD = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def _md5(text):
    return hashlib.md5(text.encode("utf8")).hexdigest()


def _digest_fields(header):
    fields = {}
    for match in _DIGEST_FIELD.finditer(header):
        fields[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return fields


class Request(object):
    __slots__ = ("method", "uri", "version", "headers", "body")

    def __init__(self, method, uri, version, headers, body=b""):
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    @property
    def cseq(self):
        return self.header("cseq", "0")


class RtspSession(object):
    """One SETUP/PLAY session: a subscriber plus its transport."""

    _counter = 0

    def __init__(self, source, connection):
        RtspSession._counter += 1
        self.id = "{:08X}".format(
            (int(time.time()) ^ (RtspSession._counter << 16) ^ os.getpid()) & 0xFFFFFFFF
        )
        self.source = source
        self.connection = connection
        self.subscriber = None
        self.packetizer = None
        self.task = None
        self.playing = False
        self.last_activity = time.monotonic()

        # TCP interleaved
        self.interleaved = None  # (rtp_channel, rtcp_channel)
        # UDP
        self.udp_socket = None
        self.udp_rtcp_socket = None
        self.client_addr = None
        self.client_rtp_port = None
        self.client_rtcp_port = None
        self.server_rtp_port = None
        self.server_rtcp_port = None

    def transport_is_tcp(self):
        return self.interleaved is not None

    async def close(self):
        self.playing = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        if self.subscriber is not None:
            self.source.unsubscribe(self.subscriber)
            self.subscriber = None
        for sock in (self.udp_socket, self.udp_rtcp_socket):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        self.udp_socket = self.udp_rtcp_socket = None


class RtspConnection(object):
    """Handles one client TCP connection."""

    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.sessions = {}
        self._write_lock = asyncio.Lock()
        self._authenticated = not server.requires_auth
        self._nonce = None

    # ------------------------------------------------------------------ #

    async def serve(self):
        try:
            while True:
                request = await self._read_message()
                if request is None:
                    break
                await self._handle(request)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("%s: connection handler failed", self._peer_str())
        finally:
            for session in list(self.sessions.values()):
                await session.close()
            self.sessions.clear()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            _LOG.info("%s: disconnected", self._peer_str())

    def _peer_str(self):
        if not self.peer:
            return "?"
        return "{}:{}".format(self.peer[0], self.peer[1])

    async def _read_message(self):
        """Read one RTSP request, transparently skipping interleaved data."""
        while True:
            first = await self.reader.readexactly(1)
            if first == b"$":
                # Interleaved binary from the client (usually RTCP RR) - drop it.
                head = await self.reader.readexactly(3)
                length = struct.unpack("!H", head[1:3])[0]
                if length:
                    await self.reader.readexactly(length)
                continue

            line = first + await self.reader.readuntil(b"\r\n")
            request_line = line.decode("iso-8859-1").strip()
            if not request_line:
                continue

            parts = request_line.split()
            if len(parts) != 3:
                _LOG.debug("%s: malformed request line %r", self._peer_str(), request_line)
                return None
            method, uri, version = parts

            headers = {}
            while True:
                raw = await self.reader.readuntil(b"\r\n")
                text = raw.decode("iso-8859-1").strip()
                if not text:
                    break
                if ":" not in text:
                    continue
                key, value = text.split(":", 1)
                headers[key.strip().lower()] = value.strip()

            body = b""
            length = int(headers.get("content-length", 0) or 0)
            if length:
                body = await self.reader.readexactly(length)

            return Request(method.upper(), uri, version, headers, body)

    async def _send(self, data):
        async with self._write_lock:
            self.writer.write(data)
            await self.writer.drain()

    async def _respond(self, request, status=200, headers=None, body=b"",
                       extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf8")
        lines = [
            "{} {} {}".format(RTSP_VERSION, status, _STATUS.get(status, "Error")),
            "CSeq: {}".format(request.cseq if request else 0),
            "Server: {}".format(SERVER_NAME),
            "Date: {}".format(
                time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
            ),
        ]
        for key, value in (headers or {}).items():
            lines.append("{}: {}".format(key, value))
        for key, value in extra_headers or ():
            lines.append("{}: {}".format(key, value))
        if body:
            lines.append("Content-Length: {}".format(len(body)))
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + body
        await self._send(payload)

    # ------------------------------------------------------------------ #

    def _check_auth(self, request):
        """Accept either Basic or Digest against any configured user."""
        if self._authenticated:
            return True
        users = self.server.users
        if not users:
            return True

        header = request.header("authorization", "")
        if not header:
            return False
        scheme = header.split(" ", 1)[0].lower()

        if scheme == "basic":
            try:
                decoded = base64.b64decode(header[6:]).decode("utf8")
            except Exception:
                return False
            if ":" not in decoded:
                return False
            user, password = decoded.split(":", 1)
            if users.get(user) == password:
                self._authenticated = True
                return True
            _LOG.warning("%s: bad Basic credentials for %r", self._peer_str(), user)
            return False

        if scheme == "digest":
            fields = _digest_fields(header)
            user = fields.get("username")
            password = users.get(user)
            if password is None:
                _LOG.warning("%s: unknown user %r", self._peer_str(), user)
                return False
            if fields.get("nonce") != self._nonce:
                return False
            ha1 = _md5("{}:{}:{}".format(user, self.server.realm, password))
            ha2 = _md5("{}:{}".format(request.method, fields.get("uri", request.uri)))
            expected = _md5("{}:{}:{}".format(ha1, self._nonce, ha2))
            if expected == fields.get("response"):
                self._authenticated = True
                return True
            _LOG.warning("%s: bad Digest response for %r", self._peer_str(), user)
        return False

    async def _handle(self, request):
        if request.method != "OPTIONS" and not self._check_auth(request):
            if self._nonce is None:
                self._nonce = os.urandom(16).hex()
            await self._respond(
                request,
                401,
                {
                    "WWW-Authenticate": 'Digest realm="{}", nonce="{}"'.format(
                        self.server.realm, self._nonce
                    ),
                },
                extra_headers=[
                    ("WWW-Authenticate", 'Basic realm="{}"'.format(self.server.realm))
                ],
            )
            return

        handler = {
            "OPTIONS": self._do_options,
            "DESCRIBE": self._do_describe,
            "SETUP": self._do_setup,
            "PLAY": self._do_play,
            "PAUSE": self._do_pause,
            "TEARDOWN": self._do_teardown,
            "GET_PARAMETER": self._do_get_parameter,
            "SET_PARAMETER": self._do_get_parameter,
        }.get(request.method)

        if handler is None:
            await self._respond(request, 405)
            return
        await handler(request)

    # ------------------------------------------------------------------ #

    async def _do_options(self, request):
        await self._respond(
            request,
            200,
            {
                "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, "
                "GET_PARAMETER, SET_PARAMETER"
            },
        )

    def _source_for(self, uri):
        path = urlparse(uri).path or "/"
        name = unquote(path.strip("/").split("/")[0])
        if not name:
            return None
        return self.server.sources.get(name)

    async def _do_describe(self, request):
        source = self._source_for(request.uri)
        if source is None:
            await self._respond(request, 404)
            return

        ready = await source.wait_ready(self.server.describe_timeout)
        if not ready or source.params is None or not source.params.ready:
            _LOG.warning(
                "%s: %s not ready for DESCRIBE (%s)",
                self._peer_str(),
                source.name,
                source.last_error or "no keyframe yet",
            )
            await self._respond(request, 503, {"Retry-After": "5"})
            return

        sdp = self._build_sdp(source, request.uri)
        await self._respond(
            request,
            200,
            {"Content-Type": "application/sdp", "Content-Base": request.uri.rstrip("/") + "/"},
            sdp,
        )

    def _build_sdp(self, source, uri):
        params = source.params
        lines = [
            "v=0",
            "o=- {} 1 IN IP4 127.0.0.1".format(int(time.time())),
            "s={}".format(source.name),
            "i={} via reolink2rtsp".format(source.config.host),
            "c=IN IP4 0.0.0.0",
            "t=0 0",
            "a=tool:{}".format(SERVER_NAME),
            "a=type:broadcast",
            "a=control:*",
            "a=range:npt=now-",
            "m=video 0 RTP/AVP {}".format(PAYLOAD_TYPE),
            "a=rtpmap:{} {}".format(PAYLOAD_TYPE, params.rtpmap()),
            params.fmtp(PAYLOAD_TYPE),
            "a=control:trackID=0",
            "a=recvonly",
        ]
        return "\r\n".join(lines) + "\r\n"

    # ------------------------------------------------------------------ #

    async def _do_setup(self, request):
        source = self._source_for(request.uri)
        if source is None:
            await self._respond(request, 404)
            return

        transport = request.header("transport", "")
        session = RtspSession(source, self)

        lower = transport.lower()
        if "rtp/avp/tcp" in lower or "interleaved" in lower:
            channels = (0, 1)
            for field in transport.split(";"):
                field = field.strip()
                if field.lower().startswith("interleaved="):
                    try:
                        values = field.split("=", 1)[1].split("-")
                        channels = (int(values[0]), int(values[1]) if len(values) > 1 else int(values[0]) + 1)
                    except (ValueError, IndexError):
                        pass
            session.interleaved = channels
            response_transport = "RTP/AVP/TCP;unicast;interleaved={}-{}".format(*channels)

        elif "rtp/avp" in lower:
            client_ports = None
            for field in transport.split(";"):
                field = field.strip()
                if field.lower().startswith("client_port="):
                    try:
                        values = field.split("=", 1)[1].split("-")
                        client_ports = (
                            int(values[0]),
                            int(values[1]) if len(values) > 1 else int(values[0]) + 1,
                        )
                    except (ValueError, IndexError):
                        pass
            if client_ports is None:
                await self._respond(request, 461)
                return

            try:
                rtp_sock, rtcp_sock, rtp_port, rtcp_port = _bind_udp_pair(
                    self.server.bind
                )
            except OSError as exc:
                _LOG.error("could not bind UDP ports: %s", exc)
                await self._respond(request, 500)
                return

            session.udp_socket = rtp_sock
            session.udp_rtcp_socket = rtcp_sock
            session.server_rtp_port = rtp_port
            session.server_rtcp_port = rtcp_port
            session.client_addr = self.peer[0] if self.peer else "127.0.0.1"
            session.client_rtp_port = client_ports[0]
            session.client_rtcp_port = client_ports[1]
            response_transport = (
                "RTP/AVP;unicast;client_port={}-{};server_port={}-{};ssrc={:08X}".format(
                    client_ports[0], client_ports[1], rtp_port, rtcp_port, 0x5245_4F4C
                )
            )
        else:
            await self._respond(request, 461)
            return

        self.sessions[session.id] = session
        _LOG.info(
            "%s: SETUP %s over %s",
            self._peer_str(),
            source.name,
            "TCP" if session.transport_is_tcp() else "UDP",
        )
        await self._respond(
            request,
            200,
            {
                "Session": "{};timeout={}".format(session.id, SESSION_TIMEOUT),
                "Transport": response_transport,
            },
        )

    def _session_for(self, request):
        header = request.header("session", "")
        sid = header.split(";")[0].strip()
        return self.sessions.get(sid)

    async def _do_play(self, request):
        session = self._session_for(request)
        if session is None:
            await self._respond(request, 454)
            return
        if session.playing:
            await self._respond(request, 200, {"Session": session.id})
            return

        source = session.source
        session.subscriber = source.subscribe()
        session.packetizer = RtpPacketizer(
            source.codec or "H264", payload_type=PAYLOAD_TYPE, mtu=self.server.mtu
        )
        session.playing = True
        session.task = asyncio.ensure_future(self._pump(session))

        _LOG.info("%s: PLAY %s", self._peer_str(), source.name)
        await self._respond(
            request,
            200,
            {
                "Session": session.id,
                "Range": "npt=now-",
                "RTP-Info": "url={};seq={};rtptime={}".format(
                    request.uri.rstrip("/"), session.packetizer.sequence, 0
                ),
            },
        )

    async def _do_pause(self, request):
        session = self._session_for(request)
        if session is None:
            await self._respond(request, 454)
            return
        session.playing = False
        await self._respond(request, 200, {"Session": session.id})

    async def _do_teardown(self, request):
        session = self._session_for(request)
        if session is not None:
            await session.close()
            self.sessions.pop(session.id, None)
        await self._respond(request, 200)

    async def _do_get_parameter(self, request):
        session = self._session_for(request)
        headers = {"Session": session.id} if session else {}
        if session is not None:
            session.last_activity = time.monotonic()
        await self._respond(request, 200, headers)

    # ------------------------------------------------------------------ #

    async def _pump(self, session):
        """Pull access units and push RTP until the session ends."""
        packetizer = session.packetizer
        subscriber = session.subscriber
        loop = asyncio.get_event_loop()
        try:
            while session.playing:
                unit = await subscriber.queue.get()
                if unit is None:
                    break
                packets = packetizer.packetize(unit.nals, unit.timestamp)
                if session.transport_is_tcp():
                    blob = bytearray()
                    channel = session.interleaved[0]
                    for packet in packets:
                        blob += b"$" + bytes([channel]) + struct.pack("!H", len(packet))
                        blob += packet
                    await self._send(bytes(blob))
                else:
                    target = (session.client_addr, session.client_rtp_port)
                    for packet in packets:
                        try:
                            await loop.sock_sendto(session.udp_socket, packet, target)
                        except (BlockingIOError, InterruptedError):
                            pass
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError):
            _LOG.info("%s: client went away", self._peer_str())
        except Exception:
            _LOG.exception("%s: RTP pump failed", self._peer_str())
        finally:
            session.playing = False


def _bind_udp_pair(bind_addr, first=20000, last=30000):
    """Bind an even RTP port and the odd RTCP port above it."""
    for port in range(first, last, 2):
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtp.bind((bind_addr, port))
            rtcp.bind((bind_addr, port + 1))
        except OSError:
            rtp.close()
            rtcp.close()
            continue
        rtp.setblocking(False)
        rtcp.setblocking(False)
        return rtp, rtcp, port, port + 1
    raise OSError("no free UDP port pair in range {}-{}".format(first, last))


class RtspServer(object):
    """Serves one or more camera sources on a single TCP port."""

    def __init__(self, sources, bind="0.0.0.0", port=554, mtu=1400,
                 users=None, realm=SERVER_NAME, describe_timeout=20.0):
        self.sources = sources  # {path: CameraSource}
        self.bind = bind
        self.port = port
        self.mtu = mtu
        self.users = dict(users or {})
        self.realm = realm
        self.describe_timeout = describe_timeout
        self._server = None

    @property
    def requires_auth(self):
        return bool(self.users)

    async def start(self):
        try:
            self._server = await asyncio.start_server(
                self._on_client, self.bind, self.port
            )
        except PermissionError:
            raise PermissionError(
                "cannot bind port {} (ports below 1024 need root; either run "
                "with privileges, grant CAP_NET_BIND_SERVICE, or set base_port "
                "to 8554 in the config)".format(self.port)
            )
        for name, source in sorted(self.sources.items()):
            _LOG.info(
                "serving %s  ->  %s:%s (%s stream)%s",
                source.config.describe(self.bind),
                source.config.host,
                source.config.port,
                source.config.stream,
                "" if self.users else "  [no auth]",
            )

    async def serve_forever(self):
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for source in self.sources.values():
            await source.shutdown()

    async def _on_client(self, reader, writer):
        connection = RtspConnection(self, reader, writer)
        _LOG.info("%s: connected", connection._peer_str())
        await connection.serve()
