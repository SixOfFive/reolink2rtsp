"""Asyncio client for Reolink's proprietary "Baichuan" protocol (TCP port 9000).

Cameras such as the Reolink Lumus E430 expose *no* HTTP, RTSP or ONVIF service -
port 9000 is the only thing listening. This module speaks the protocol their own
app uses, which is enough to log in and pull a live H.264/H.265 elementary
stream off the camera.

Wire format
-----------
Every message starts with a header, little-endian throughout::

    offset  size  field
    0       4     magic 0xf0debc0a
    4       4     msg_id          (1 = login, 3 = video start, 4 = video stop, ...)
    8       4     body_len        (bytes following the header)
    12      1     channel_id
    13      1     stream_type     (0 = clear/main, 1 = fluent/sub)
    14      2     msg_num         (request/response correlation)
    16      2     response_code   (0 when sending; 200 = OK) / encryption tag on legacy
    18      2     message class   (0x1464 or 0x0000 = 24-byte header,
                                   0x1466 = 20-byte header,
                                   0x1465 = 20-byte legacy header)
    20      4     payload_offset  (24-byte headers only)

The body is split at ``payload_offset``: the first part is an XML "extension"
describing what follows, the remainder is the payload. XML is encrypted (see
:mod:`.crypto`); binary media payloads are **not**.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from xml.etree import ElementTree

from .crypto import (
    aes_cfb_decrypt,
    aes_cfb_encrypt,
    bc_crypt,
    derive_aes_key,
    md5_str_modern,
)

_LOG = logging.getLogger(__name__)

# On the wire the magic is literally the bytes f0 de bc 0a, which read back as a
# little-endian u32 is 0x0abcdef0. Defining the bytes first keeps that straight.
MAGIC_BYTES = b"\xf0\xde\xbc\x0a"
MAGIC = struct.unpack("<I", MAGIC_BYTES)[0]
DEFAULT_PORT = 9000

# Message ids we use.
MSG_LOGIN = 1
MSG_LOGOUT = 2
MSG_VIDEO = 3
MSG_VIDEO_STOP = 4
MSG_PING = 93

# Message classes. These are read/written as little-endian u16, so the constant
# looks byte-swapped compared to the bytes on the wire: class bytes `14 64`
# become 0x6414.
CLASS_MODERN = 0x6414  # wire 14 64 -> 24-byte header
CLASS_MODERN_SHORT = 0x6614  # wire 14 66 -> 20-byte header
CLASS_LEGACY = 0x6514  # wire 14 65 -> 20-byte legacy header

# Channel id for host-scoped commands. These cameras are single-channel, so 0 is
# both correct and what the official client sends. It doubles as the XOR offset
# for BC-encrypted bodies.
CH_HOST = 0

STREAM_MAIN = "main"
STREAM_SUB = "sub"
STREAM_EXTERN = "extern"

# stream_type header byte, Preview <handle>, and the XML stream name.
_STREAMS = {
    STREAM_MAIN: (0, 0, "mainStream"),
    STREAM_SUB: (1, 256, "subStream"),
    STREAM_EXTERN: (0, 1024, "externStream"),
}

XML_DECL = '<?xml version="1.0" encoding="UTF-8" ?>\n'

LOGIN_XML = XML_DECL + (
    "<body>\n"
    "<LoginUser version=\"1.1\">\n"
    "<userName>{user_hash}</userName>\n"
    "<password>{password_hash}</password>\n"
    "<userVer>1</userVer>\n"
    "</LoginUser>\n"
    "<LoginNet version=\"1.1\">\n"
    "<type>LAN</type>\n"
    "<udpPort>0</udpPort>\n"
    "</LoginNet>\n"
    "</body>\n"
)

PREVIEW_XML = XML_DECL + (
    "<body>\n"
    "<Preview version=\"1.1\">\n"
    "<channelId>{channel}</channelId>\n"
    "<handle>{handle}</handle>\n"
    "<streamType>{stream_name}</streamType>\n"
    "</Preview>\n"
    "</body>\n"
)

PREVIEW_STOP_XML = XML_DECL + (
    "<body>\n"
    "<Preview version=\"1.1\">\n"
    "<channelId>{channel}</channelId>\n"
    "<handle>{handle}</handle>\n"
    "</Preview>\n"
    "</body>\n"
)


class BaichuanError(Exception):
    """Any protocol-level failure."""


class LoginFailed(BaichuanError):
    """Camera rejected the credentials."""


class Message(object):
    """A parsed Baichuan message."""

    __slots__ = (
        "msg_id",
        "channel_id",
        "stream_type",
        "msg_num",
        "response_code",
        "msg_class",
        "header_len",
        "extension",
        "payload",
    )

    def __init__(
        self,
        msg_id,
        channel_id,
        stream_type,
        msg_num,
        response_code,
        msg_class,
        header_len,
        extension,
        payload,
    ):
        self.msg_id = msg_id
        self.channel_id = channel_id
        self.stream_type = stream_type
        self.msg_num = msg_num
        # Only meaningful on 24-byte headers. On 20-byte headers this field is
        # the encryption tag instead (0xdd12 = BC, 0xdd02/0xdd03 = AES).
        self.response_code = response_code
        self.msg_class = msg_class
        self.header_len = header_len
        self.extension = extension  # raw (still encrypted) bytes before payload_offset
        self.payload = payload  # raw bytes after payload_offset

    @property
    def has_status(self):
        """True when response_code really is an HTTP-like status."""
        return self.header_len == 24

    @property
    def enc_tag(self):
        """Encryption tag from a 20-byte header, else None."""
        return None if self.header_len == 24 else self.response_code

    def __repr__(self):
        return (
            "<Message id={} ch={} stream={} num={} code={} class=0x{:04x} "
            "ext={}B payload={}B>".format(
                self.msg_id,
                self.channel_id,
                self.stream_type,
                self.msg_num,
                self.response_code,
                self.msg_class,
                len(self.extension),
                len(self.payload),
            )
        )


def _header_len(msg_class):
    return 24 if msg_class in (CLASS_MODERN, 0x0000) else 20


class BaichuanClient(object):
    """A logged-in connection to one camera."""

    def __init__(self, host, username, password, port=DEFAULT_PORT, timeout=15.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

        self._reader = None
        self._writer = None
        self._read_task = None
        self._msg_num = 0
        self._aes_key = None
        self._logged_in = False
        self._nonce = None

        # (msg_id, msg_num) -> Future waiting on the reply
        self._pending = {}
        # msg_num -> asyncio.Queue for streaming video payloads
        self._video_queues = {}
        self._closed = asyncio.Event()
        self.device_info = {}

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    @property
    def connected(self):
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self):
        if self.connected:
            return
        _LOG.debug("%s: connecting to port %s", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        self._closed = asyncio.Event()
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def close(self):
        if self._logged_in:
            try:
                await asyncio.wait_for(self.logout(), 3)
            except Exception:
                pass
        self._logged_in = False
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None
        self._closed.set()

    def _next_msg_num(self):
        self._msg_num = (self._msg_num + 1) % 0x10000
        return self._msg_num

    # ------------------------------------------------------------------ #
    # Framing
    # ------------------------------------------------------------------ #

    def _build(
        self,
        msg_id,
        body=b"",
        channel_id=CH_HOST,
        stream_type=0,
        msg_num=None,
        msg_class=CLASS_MODERN,
        payload_offset=0,
        legacy_tag=0x0000,
    ):
        if msg_num is None:
            msg_num = self._next_msg_num()
        header = struct.pack(
            "<IIIBBHHH",
            MAGIC,
            msg_id,
            len(body),
            channel_id & 0xFF,
            stream_type & 0xFF,
            msg_num,
            legacy_tag,
            msg_class,
        )
        if _header_len(msg_class) == 24:
            header += struct.pack("<I", payload_offset)
        return header + body, msg_num

    async def _write(self, data):
        if self._writer is None:
            raise BaichuanError("{}: not connected".format(self.host))
        self._writer.write(data)
        await self._writer.drain()

    async def _read_exactly(self, count):
        return await self._reader.readexactly(count)

    async def _read_loop(self):
        """Read messages forever, dispatching to waiters and video queues."""
        try:
            while True:
                head = await self._read_exactly(20)
                if head[0:4] != MAGIC_BYTES:
                    # Re-sync: scan forward for the magic. Should not happen on a
                    # healthy connection, but a desync would otherwise wedge us.
                    _LOG.warning(
                        "%s: bad magic %s, resyncing", self.host, head[0:4].hex()
                    )
                    head = await self._resync(head)

                (msg_id, body_len, channel_id, stream_type, msg_num, code, msg_class) = (
                    struct.unpack("<IIBBHHH", head[4:20])
                )

                header_len = _header_len(msg_class)
                payload_offset = 0
                if header_len == 24:
                    payload_offset = struct.unpack("<I", await self._read_exactly(4))[0]

                body = await self._read_exactly(body_len) if body_len else b""

                if payload_offset == 0 or payload_offset > body_len:
                    extension, payload = body, b""
                else:
                    extension, payload = body[:payload_offset], body[payload_offset:]

                msg = Message(
                    msg_id,
                    channel_id,
                    stream_type,
                    msg_num,
                    code,
                    msg_class,
                    header_len,
                    extension,
                    payload,
                )
                self._dispatch(msg)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            _LOG.debug("%s: connection closed (%s)", self.host, exc)
            self._fail_all(BaichuanError("{}: connection lost".format(self.host)))
        except asyncio.CancelledError:
            self._fail_all(BaichuanError("{}: client shut down".format(self.host)))
            raise
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.exception("%s: reader crashed", self.host)
            self._fail_all(exc)
        finally:
            self._closed.set()

    async def _resync(self, head):
        """Scan the byte stream until the magic reappears, return a fresh header."""
        window = bytearray(head)
        while True:
            idx = bytes(window).find(MAGIC_BYTES)
            if idx >= 0:
                window = window[idx:]
                while len(window) < 20:
                    window += await self._read_exactly(20 - len(window))
                return bytes(window)
            window = window[-3:] + bytearray(await self._read_exactly(64))

    def _dispatch(self, msg):
        # Video payloads stream in under the msg_num of the original request.
        queue = self._video_queues.get(msg.msg_num)
        if queue is not None and msg.msg_id == MSG_VIDEO:
            if msg.payload:
                queue.put_nowait(msg.payload)
            elif msg.extension and not (
                msg.has_status and msg.response_code not in (0, 200, 201, 300)
            ):
                # Some firmwares put the media bytes in the body with no
                # payload_offset set at all.
                queue.put_nowait(msg.extension)
            return

        future = self._pending.pop((msg.msg_id, msg.msg_num), None)
        if future is not None and not future.done():
            future.set_result(msg)
            return

        if msg.msg_id == 234:  # heartbeat from the camera
            asyncio.ensure_future(self._pong(msg.msg_num))
            return

        _LOG.debug("%s: unsolicited %r", self.host, msg)

    async def _pong(self, msg_num):
        try:
            data, _ = self._build(234, msg_num=msg_num, msg_class=CLASS_MODERN)
            await self._write(data)
        except Exception:
            pass

    def _fail_all(self, exc):
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        for queue in list(self._video_queues.values()):
            queue.put_nowait(None)  # sentinel: stream ended

    # ------------------------------------------------------------------ #
    # Request/response
    # ------------------------------------------------------------------ #

    def _decrypt(self, msg, force_bc=False):
        """Decrypt a message body to text. Returns '' for empty bodies."""
        blob = msg.extension
        if not blob:
            return ""

        candidates = []
        if force_bc or self._aes_key is None:
            candidates.append(lambda: bc_crypt(blob, msg.channel_id))
        else:
            candidates.append(lambda: aes_cfb_decrypt(blob, self._aes_key))
            candidates.append(lambda: bc_crypt(blob, msg.channel_id))
        candidates.append(lambda: blob)

        for attempt in candidates:
            try:
                text = attempt().decode("utf8")
            except (UnicodeDecodeError, ValueError):
                continue
            if text.startswith("<?xml"):
                return text
        raise BaichuanError(
            "{}: could not decrypt reply to msg_id {} (starts with {})".format(
                self.host, msg.msg_id, blob[:8].hex()
            )
        )

    async def send(
        self,
        msg_id,
        body="",
        channel_id=CH_HOST,
        stream_type=0,
        encrypt="aes",
        msg_class=CLASS_MODERN,
        legacy_tag=0x0000,
        expect_reply=True,
    ):
        """Send a command and await its reply."""
        if isinstance(body, str):
            raw = body.encode("utf8")
        else:
            raw = body

        if raw:
            if encrypt == "bc":
                enc = bc_crypt(raw, channel_id)
            elif encrypt == "aes":
                if self._aes_key is None:
                    raise BaichuanError("{}: AES requested before login".format(self.host))
                enc = aes_cfb_encrypt(raw, self._aes_key)
            else:
                enc = raw
        else:
            enc = b""

        data, msg_num = self._build(
            msg_id,
            enc,
            channel_id=channel_id,
            stream_type=stream_type,
            msg_class=msg_class,
            payload_offset=0,
            legacy_tag=legacy_tag,
        )

        future = None
        if expect_reply:
            future = asyncio.get_event_loop().create_future()
            self._pending[(msg_id, msg_num)] = future

        try:
            await self._write(data)
        except Exception:
            self._pending.pop((msg_id, msg_num), None)
            raise

        if not expect_reply:
            return msg_num, None

        try:
            msg = await asyncio.wait_for(future, self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop((msg_id, msg_num), None)
            raise BaichuanError(
                "{}: timeout waiting for reply to msg_id {}".format(self.host, msg_id)
            )

        if msg.has_status and msg.response_code not in (0, 200, 201, 300):
            if msg.response_code == 401:
                raise LoginFailed(
                    "{}: 401 unauthorized - check the camera username/password".format(
                        self.host
                    )
                )
            raise BaichuanError(
                "{}: msg_id {} returned status {}".format(
                    self.host, msg_id, msg.response_code
                )
            )
        return msg_num, msg

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #

    async def _get_nonce(self):
        """A header-only legacy message makes the camera hand back a nonce."""
        _, msg = await self.send(
            MSG_LOGIN,
            b"",
            channel_id=CH_HOST,
            encrypt="none",
            msg_class=CLASS_LEGACY,
            legacy_tag=0xDC12,  # bytes 0x12 0xdc on the wire
        )
        text = self._decrypt(msg, force_bc=True)
        root = ElementTree.fromstring(text)
        node = root.find(".//nonce")
        if node is None or not node.text:
            raise BaichuanError("{}: no nonce in login challenge".format(self.host))
        return node.text

    async def login(self):
        if self._logged_in:
            return
        await self.connect()

        nonce = await self._get_nonce()
        self._nonce = nonce

        body = LOGIN_XML.format(
            user_hash=md5_str_modern(self.username + nonce),
            password_hash=md5_str_modern(self.password + nonce),
        )
        _, msg = await self.send(MSG_LOGIN, body, channel_id=CH_HOST, encrypt="bc")

        self._aes_key = derive_aes_key(nonce, self.password)
        self._logged_in = True

        try:
            text = self._decrypt(msg, force_bc=True)
            self.device_info = _parse_device_info(text)
        except Exception:
            self.device_info = {}
        _LOG.info(
            "%s: logged in%s",
            self.host,
            " ({})".format(self.device_info.get("type", ""))
            if self.device_info.get("type")
            else "",
        )

    async def logout(self):
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            await self.send(MSG_LOGOUT, "", expect_reply=False)
        except Exception:
            pass

    async def ping(self):
        """Keepalive. The camera drops idle connections after ~30s."""
        await self.send(MSG_PING, "")

    # ------------------------------------------------------------------ #
    # Video
    # ------------------------------------------------------------------ #

    async def start_video(self, stream=STREAM_MAIN, channel=0, queue_size=256):
        """Ask the camera to start pushing video.

        Returns ``(msg_num, queue)``. The queue yields raw BcMedia byte chunks
        and a final ``None`` when the stream ends.
        """
        if stream not in _STREAMS:
            raise ValueError("unknown stream {!r}".format(stream))
        stream_code, handle, stream_name = _STREAMS[stream]

        queue = asyncio.Queue(maxsize=queue_size)
        body = PREVIEW_XML.format(
            channel=channel, handle=handle, stream_name=stream_name
        )

        # Register the queue before sending: the camera can start pushing media
        # the moment it accepts the command.
        msg_num = self._msg_num + 1
        self._video_queues[msg_num % 0x10000] = queue
        try:
            sent_num, _ = await self.send(
                MSG_VIDEO,
                body,
                channel_id=channel,
                stream_type=stream_code,
                encrypt="aes",
            )
        except Exception:
            self._video_queues.pop(msg_num % 0x10000, None)
            raise

        if sent_num != msg_num % 0x10000:  # pragma: no cover - ordering guard
            self._video_queues.pop(msg_num % 0x10000, None)
            self._video_queues[sent_num] = queue

        _LOG.info("%s: %s stream started (msg_num=%s)", self.host, stream_name, sent_num)
        return sent_num, queue

    async def stop_video(self, msg_num, stream=STREAM_MAIN, channel=0):
        self._video_queues.pop(msg_num, None)
        _, handle, _ = _STREAMS[stream]
        body = PREVIEW_STOP_XML.format(channel=channel, handle=handle)
        try:
            await self.send(
                MSG_VIDEO_STOP,
                body,
                channel_id=channel,
                encrypt="aes",
                expect_reply=False,
            )
        except Exception:
            pass


def _parse_device_info(text):
    """Pull the interesting bits out of the login reply."""
    info = {}
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return info
    node = root.find(".//DeviceInfo")
    if node is None:
        return info
    for child in node:
        if child.text:
            info[child.tag] = child.text.strip()
    return info
