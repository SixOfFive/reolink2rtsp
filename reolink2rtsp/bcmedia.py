"""Parser for the "BcMedia" container Reolink wraps its media frames in.

Once a ``Preview`` command is accepted, the camera pushes a byte stream made of
self-describing chunks. Each starts with a 4-byte ASCII magic:

===========  =========================================================
``1001``     stream info (width/height/fps)         - 32 bytes total
``1002``     stream info, v2                        - 32 bytes total
``N0dc``     I-frame, N is an ASCII digit 0-9
``N1dc``     P-frame, N is an ASCII digit 0-9
``05wb``     AAC audio
``01wb``     ADPCM audio
===========  =========================================================

Video chunks carry a 4-byte codec tag (``H264``/``H265``), the payload size, and
a variable extra-header size; payloads are padded up to an 8-byte boundary.

The parser is a feed/pull state machine so it can sit directly on a socket
without needing the whole stream in memory.
"""

from __future__ import annotations

import struct

__all__ = ["BcMediaParser", "VideoFrame", "AudioFrame", "StreamInfo"]

PAD = 8

MAGIC_INFO_V1 = b"1001"
MAGIC_INFO_V2 = b"1002"
MAGIC_AAC = b"05wb"
MAGIC_ADPCM = b"01wb"

# I-frames are "00dc".."90dc", P-frames "01dc".."91dc".
_IFRAME_TAIL = b"0dc"
_PFRAME_TAIL = b"1dc"

_CODECS = (b"H264", b"H265")


class StreamInfo(object):
    __slots__ = ("width", "height", "fps")

    def __init__(self, width, height, fps):
        self.width = width
        self.height = height
        self.fps = fps

    def __repr__(self):
        return "<StreamInfo {}x{} @{}fps>".format(self.width, self.height, self.fps)


class VideoFrame(object):
    __slots__ = ("codec", "keyframe", "microseconds", "timestamp", "data")

    def __init__(self, codec, keyframe, microseconds, timestamp, data):
        self.codec = codec  # "H264" or "H265"
        self.keyframe = keyframe
        self.microseconds = microseconds
        self.timestamp = timestamp
        self.data = data  # Annex-B elementary stream bytes

    def __repr__(self):
        return "<{}{} {} bytes>".format(
            self.codec, " key" if self.keyframe else "", len(self.data)
        )


class AudioFrame(object):
    __slots__ = ("codec", "data")

    def __init__(self, codec, data):
        self.codec = codec  # "AAC" or "ADPCM"
        self.data = data


def _padding(size):
    remainder = size % PAD
    return 0 if remainder == 0 else PAD - remainder


class _Incomplete(Exception):
    """Not enough buffered bytes yet."""


class BcMediaParser(object):
    """Feed bytes in, pull parsed frames out."""

    def __init__(self, on_desync=None):
        self._buf = bytearray()
        self._on_desync = on_desync
        self.info = None

    def feed(self, data):
        """Add raw bytes and return every complete frame now available."""
        if data:
            self._buf += data
        frames = []
        while True:
            try:
                frame = self._parse_one()
            except _Incomplete:
                break
            if frame is not None:
                frames.append(frame)
        return frames

    # -------------------------------------------------------------- #

    def _need(self, count):
        if len(self._buf) < count:
            raise _Incomplete()

    def _parse_one(self):
        self._need(4)
        magic = bytes(self._buf[0:4])

        if magic == MAGIC_INFO_V1 or magic == MAGIC_INFO_V2:
            return self._parse_info()
        if magic[1:4] == _IFRAME_TAIL and 0x30 <= magic[0] <= 0x39:
            return self._parse_video(keyframe=True)
        if magic[1:4] == _PFRAME_TAIL and 0x30 <= magic[0] <= 0x39:
            return self._parse_video(keyframe=False)
        if magic == MAGIC_AAC:
            return self._parse_audio("AAC")
        if magic == MAGIC_ADPCM:
            return self._parse_adpcm()

        # Unknown magic - hunt for the next recognisable chunk boundary.
        self._resync()
        return None

    def _resync(self):
        if self._on_desync is not None:
            self._on_desync(bytes(self._buf[0:8]))
        for idx in range(1, len(self._buf) - 3):
            candidate = bytes(self._buf[idx : idx + 4])
            if (
                candidate in (MAGIC_INFO_V1, MAGIC_INFO_V2, MAGIC_AAC, MAGIC_ADPCM)
                or (
                    candidate[1:4] in (_IFRAME_TAIL, _PFRAME_TAIL)
                    and 0x30 <= candidate[0] <= 0x39
                )
            ):
                del self._buf[0:idx]
                return
        # Nothing found; keep the tail in case a magic straddles the boundary.
        if len(self._buf) > 3:
            del self._buf[0 : len(self._buf) - 3]
        raise _Incomplete()

    def _parse_info(self):
        self._need(32)
        header_size = struct.unpack_from("<I", self._buf, 4)[0]
        if header_size != 32:
            self._resync()
            return None
        width, height = struct.unpack_from("<II", self._buf, 8)
        fps = self._buf[17]
        del self._buf[0:32]
        self.info = StreamInfo(width, height, fps)
        return self.info

    def _parse_video(self, keyframe):
        self._need(24)
        codec = bytes(self._buf[4:8])
        if codec not in _CODECS:
            self._resync()
            return None

        payload_size, extra_size, microseconds = struct.unpack_from("<III", self._buf, 8)
        header_size = 24 + extra_size

        timestamp = None
        if extra_size >= 4:
            self._need(28)
            timestamp = struct.unpack_from("<I", self._buf, 24)[0]

        pad = _padding(payload_size)
        total = header_size + payload_size + pad
        self._need(total)

        data = bytes(self._buf[header_size : header_size + payload_size])
        del self._buf[0:total]

        return VideoFrame(
            codec.decode("ascii"), keyframe, microseconds, timestamp, data
        )

    def _parse_audio(self, codec):
        self._need(8)
        payload_size = struct.unpack_from("<H", self._buf, 4)[0]
        total = 8 + payload_size + _padding(payload_size)
        self._need(total)
        data = bytes(self._buf[8 : 8 + payload_size])
        del self._buf[0:total]
        return AudioFrame(codec, data)

    def _parse_adpcm(self):
        self._need(12)
        payload_size = struct.unpack_from("<H", self._buf, 4)[0]
        if payload_size < 4:
            self._resync()
            return None
        # 8 bytes of chunk header + a 4-byte ADPCM sub-header inside the payload.
        total = 8 + payload_size + _padding(payload_size)
        self._need(total)
        data = bytes(self._buf[12 : 8 + payload_size])
        del self._buf[0:total]
        return AudioFrame("ADPCM", data)
