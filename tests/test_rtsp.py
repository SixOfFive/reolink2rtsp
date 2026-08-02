"""End-to-end test of the RTSP server against a synthetic camera source.

Starts a real RtspServer backed by a fake source that emits known H.264 access
units, then drives it with a real RTSP client over TCP-interleaved transport and
checks that every NAL comes back out byte-identical after RTP fragmentation.

No camera, no ffmpeg, no network beyond loopback.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reolink2rtsp.config import CameraConfig  # noqa: E402
from reolink2rtsp.h26x import H264, ParameterSets  # noqa: E402
from reolink2rtsp.rtsp import RtspServer  # noqa: E402
from reolink2rtsp.source import AccessUnit, Subscriber  # noqa: E402

HOST = "127.0.0.1"
PORT = 18554
USERS = {"test": "test", "frigate": "s3cret"}

SPS = bytes([0x67, 0x42, 0x00, 0x1F]) + b"\x11" * 12
PPS = bytes([0x68]) + b"\x22" * 5


def make_idr(index):
    # Deliberately larger than the MTU so it must be fragmented.
    return bytes([0x65]) + bytes([index & 0xFF]) * 3000


def make_pframe(index):
    return bytes([0x41]) + bytes([index & 0xFF]) * 200


class FakeSource(object):
    """Implements the interface RtspServer expects from CameraSource."""

    def __init__(self, name="cam"):
        self.name = name
        self.config = CameraConfig(
            name, host="10.0.0.1", rtsp_port=PORT, users=USERS, stream="main"
        )
        self.codec = H264
        self.params = ParameterSets(H264)
        self.params.observe(SPS)
        self.params.observe(PPS)
        self.last_error = None
        self.subscribers = []
        self.sent = []

    async def wait_ready(self, timeout):
        return True

    def _ensure_running(self):
        pass

    def subscribe(self):
        sub = Subscriber(maxsize=200)
        self.subscribers.append(sub)
        return sub

    def unsubscribe(self, sub):
        if sub in self.subscribers:
            self.subscribers.remove(sub)

    async def shutdown(self):
        pass

    def emit(self, count=4):
        """Push a few access units; the first must be a keyframe."""
        timestamp = 0
        for index in range(count):
            if index % 2 == 0:
                nals = [SPS, PPS, make_idr(index)]
                keyframe = True
            else:
                nals = [make_pframe(index)]
                keyframe = False
            unit = AccessUnit(nals, keyframe, timestamp, 0)
            self.sent.append(nals)
            for sub in list(self.subscribers):
                sub.offer(unit)
            timestamp += 3000


# --------------------------------------------------------------------------- #
# A real, if minimal, RTSP client
# --------------------------------------------------------------------------- #


class RtspClient(object):
    def __init__(self, host, port, path, username=None, password=None):
        self.host = host
        self.port = port
        self.path = path
        self.username = username
        self.password = password
        self.url = "rtsp://{}:{}/{}".format(host, port, path)
        self.cseq = 0
        self.session = None
        self.reader = self.writer = None
        self._auth = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def close(self):
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass

    def _auth_header(self, method):
        if self._auth is None or self.username is None:
            return None
        if self._auth["scheme"] == "basic":
            token = base64.b64encode(
                "{}:{}".format(self.username, self.password).encode()
            ).decode()
            return "Basic " + token
        realm, nonce = self._auth["realm"], self._auth["nonce"]
        ha1 = hashlib.md5(
            "{}:{}:{}".format(self.username, realm, self.password).encode()
        ).hexdigest()
        ha2 = hashlib.md5("{}:{}".format(method, self.url).encode()).hexdigest()
        response = hashlib.md5("{}:{}:{}".format(ha1, nonce, ha2).encode()).hexdigest()
        return (
            'Digest username="{}", realm="{}", nonce="{}", uri="{}", response="{}"'
        ).format(self.username, realm, nonce, self.url, response)

    async def request(self, method, extra=None, retry_auth=True):
        self.cseq += 1
        lines = ["{} {} RTSP/1.0".format(method, self.url), "CSeq: {}".format(self.cseq)]
        auth = self._auth_header(method)
        if auth:
            lines.append("Authorization: " + auth)
        if self.session:
            lines.append("Session: " + self.session)
        for key, value in (extra or {}).items():
            lines.append("{}: {}".format(key, value))
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.writer.drain()

        status, headers, body = await self._read_response()
        if status == 401 and retry_auth and self.username:
            challenge = headers.get("www-authenticate", "")
            if "digest" in challenge.lower():
                fields = dict(
                    (m.group(1), m.group(2))
                    for m in re.finditer(r'(\w+)="([^"]*)"', challenge)
                )
                self._auth = {
                    "scheme": "digest",
                    "realm": fields.get("realm", ""),
                    "nonce": fields.get("nonce", ""),
                }
            else:
                self._auth = {"scheme": "basic"}
            return await self.request(method, extra, retry_auth=False)
        return status, headers, body

    async def _read_response(self):
        # Skip any interleaved RTP that arrives while we wait for the reply.
        while True:
            first = await self.reader.readexactly(1)
            if first != b"$":
                break
            head = await self.reader.readexactly(3)
            length = struct.unpack("!H", head[1:3])[0]
            if length:
                await self.reader.readexactly(length)
        line = first + await self.reader.readuntil(b"\r\n")
        status = int(line.decode().split()[1])
        headers = {}
        while True:
            raw = await self.reader.readuntil(b"\r\n")
            text = raw.decode().strip()
            if not text:
                break
            key, value = text.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        body = b""
        length = int(headers.get("content-length", 0) or 0)
        if length:
            body = await self.reader.readexactly(length)
        return status, headers, body

    async def read_interleaved(self, count, timeout=10.0):
        """Collect *count* interleaved RTP packets."""
        packets = []
        while len(packets) < count:
            head = await asyncio.wait_for(self.reader.readexactly(4), timeout)
            assert head[0:1] == b"$", "expected interleaved frame, got {!r}".format(head)
            length = struct.unpack("!H", head[2:4])[0]
            packets.append(await asyncio.wait_for(
                self.reader.readexactly(length), timeout))
        return packets


def depacketize(packets):
    """Reassemble H.264 NALs from a list of RTP packets."""
    nals = []
    pending = None
    for packet in packets:
        payload = packet[12:]
        ntype = payload[0] & 0x1F
        if ntype == 28:  # FU-A
            fu_header = payload[1]
            if fu_header & 0x80:  # start
                original = (payload[0] & 0xE0) | (fu_header & 0x1F)
                pending = bytearray([original]) + payload[2:]
            elif pending is not None:
                pending += payload[2:]
            if fu_header & 0x40 and pending is not None:  # end
                nals.append(bytes(pending))
                pending = None
        else:
            nals.append(payload)
    return nals


# --------------------------------------------------------------------------- #


async def run():
    source = FakeSource("driveway")
    server = RtspServer(
        {"driveway": source}, bind=HOST, port=PORT, mtu=1400, users=USERS
    )
    await server.start()
    serving = asyncio.ensure_future(server.serve_forever())
    await asyncio.sleep(0.1)

    try:
        # --- unauthenticated client must be rejected -------------------- #
        anon = RtspClient(HOST, PORT, "driveway")
        await anon.connect()
        status, headers, _ = await anon.request("DESCRIBE", retry_auth=False)
        assert status == 401, "expected 401 without credentials, got {}".format(status)
        assert "www-authenticate" in headers
        await anon.close()
        print("  unauthenticated DESCRIBE rejected with 401")

        # --- wrong password must also be rejected ----------------------- #
        bad = RtspClient(HOST, PORT, "driveway", "test", "wrong")
        await bad.connect()
        status, _, _ = await bad.request("DESCRIBE")
        assert status == 401, "wrong password should fail, got {}".format(status)
        await bad.close()
        print("  wrong password rejected with 401")

        # --- unknown path -> 404 ---------------------------------------- #
        missing = RtspClient(HOST, PORT, "nosuchcam", "test", "test")
        await missing.connect()
        status, _, _ = await missing.request("DESCRIBE")
        assert status == 404, "unknown path should 404, got {}".format(status)
        await missing.close()
        print("  unknown stream path returns 404")

        # --- the real session, as the second configured user ------------ #
        client = RtspClient(HOST, PORT, "driveway", "frigate", "s3cret")
        await client.connect()

        status, headers, _ = await client.request("OPTIONS")
        assert status == 200
        assert "DESCRIBE" in headers.get("public", "")

        status, headers, body = await client.request("DESCRIBE")
        assert status == 200, "DESCRIBE failed: {}".format(status)
        sdp = body.decode()
        assert "m=video 0 RTP/AVP 96" in sdp, sdp
        assert "a=rtpmap:96 H264/90000" in sdp, sdp
        assert "profile-level-id=42001f" in sdp, sdp
        assert base64.b64encode(SPS).decode() in sdp, "SPS missing from SDP"
        assert base64.b64encode(PPS).decode() in sdp, "PPS missing from SDP"
        print("  DESCRIBE returned a well-formed SDP with parameter sets")

        status, headers, _ = await client.request(
            "SETUP", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"}
        )
        assert status == 200, "SETUP failed: {}".format(status)
        assert "interleaved=0-1" in headers.get("transport", "")
        client.session = headers["session"].split(";")[0]
        print("  SETUP negotiated TCP-interleaved transport")

        status, _, _ = await client.request("PLAY")
        assert status == 200, "PLAY failed: {}".format(status)
        print("  PLAY accepted")

        await asyncio.sleep(0.1)
        source.emit(8)

        packets = await client.read_interleaved(20)
        nals = depacketize(packets)

        assert nals[0] == SPS, "first NAL should be the SPS"
        assert nals[1] == PPS, "second NAL should be the PPS"
        expected_idr = make_idr(0)
        assert nals[2] == expected_idr, (
            "IDR did not survive FU-A fragmentation "
            "(got {} bytes, expected {})".format(len(nals[2]), len(expected_idr))
        )
        print("  {} RTP packets reassembled into {} NALs, byte-identical"
              .format(len(packets), len(nals)))

        # Sequence numbers must be contiguous.
        seqs = [struct.unpack("!H", p[2:4])[0] for p in packets]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs
        # Payload type and SSRC constant across the stream.
        assert all((p[1] & 0x7F) == 96 for p in packets)
        assert len({p[8:12] for p in packets}) == 1, "SSRC must not change"
        print("  RTP sequence numbers contiguous, PT=96, SSRC stable")

        status, _, _ = await client.request("TEARDOWN")
        assert status == 200
        await client.close()
        await asyncio.sleep(0.1)
        assert source.subscribers == [], "TEARDOWN must release the subscriber"
        print("  TEARDOWN released the source subscription")

    finally:
        serving.cancel()
        await server.stop()

    print("\nRTSP end-to-end test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
