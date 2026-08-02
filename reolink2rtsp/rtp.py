"""RTP packetisation for H.264 (RFC 6184) and H.265 (RFC 7798).

Only what a live video sender needs: single-NAL packets when a NAL fits inside
the MTU, fragmentation units when it does not. No aggregation packets - they
buy nothing for video-sized NALs and every decoder handles the other two.
"""

from __future__ import annotations

import struct

from .h26x import H265

__all__ = ["RtpPacketizer", "RTP_HEADER_LEN", "CLOCK_RATE"]

RTP_VERSION = 2
RTP_HEADER_LEN = 12
CLOCK_RATE = 90000

# Leave room for the RTP header plus the 4-byte RTSP interleave prefix inside a
# typical 1500-byte path MTU.
DEFAULT_MTU = 1400


class RtpPacketizer(object):
    """Turns NAL units into a list of RTP packets."""

    def __init__(self, codec, payload_type=96, ssrc=None, mtu=DEFAULT_MTU):
        self.codec = codec
        self.payload_type = payload_type
        self.ssrc = ssrc if ssrc is not None else 0x5245_4F4C  # "REOL"
        self.mtu = mtu
        self.sequence = 0
        self.packet_count = 0
        self.octet_count = 0

    # ------------------------------------------------------------------ #

    def _header(self, marker, timestamp):
        first = (RTP_VERSION << 6)
        second = (0x80 if marker else 0) | (self.payload_type & 0x7F)
        header = struct.pack(
            "!BBHII", first, second, self.sequence, timestamp & 0xFFFFFFFF, self.ssrc
        )
        self.sequence = (self.sequence + 1) & 0xFFFF
        return header

    def _emit(self, payload, marker, timestamp):
        self.packet_count += 1
        self.octet_count += len(payload)
        return self._header(marker, timestamp) + payload

    # ------------------------------------------------------------------ #

    def packetize(self, nals, timestamp):
        """Packetise a whole access unit.

        *nals* is a list of NAL units without start codes. The marker bit is set
        on the last packet of the access unit, as decoders expect.
        """
        packets = []
        payloads = []
        for nal in nals:
            if not nal:
                continue
            if self.codec == H265:
                payloads.extend(self._split_h265(nal))
            else:
                payloads.extend(self._split_h264(nal))

        for index, payload in enumerate(payloads):
            marker = index == len(payloads) - 1
            packets.append(self._emit(payload, marker, timestamp))
        return packets

    # ------------------------------------------------------------------ #

    def _max_payload(self):
        return self.mtu - RTP_HEADER_LEN

    def _split_h264(self, nal):
        limit = self._max_payload()
        if len(nal) <= limit:
            return [nal]

        # FU-A: indicator keeps F/NRI from the original header, type becomes 28.
        header = nal[0]
        indicator = bytes([(header & 0xE0) | 28])
        nal_type = header & 0x1F
        body = nal[1:]

        chunk_size = limit - 2  # indicator + FU header
        out = []
        offset = 0
        while offset < len(body):
            chunk = body[offset : offset + chunk_size]
            start = offset == 0
            end = offset + len(chunk) >= len(body)
            fu_header = bytes(
                [(0x80 if start else 0) | (0x40 if end else 0) | nal_type]
            )
            out.append(indicator + fu_header + chunk)
            offset += len(chunk)
        return out

    def _split_h265(self, nal):
        limit = self._max_payload()
        if len(nal) <= limit:
            return [nal]

        # H.265 has a 2-byte NAL header. FU payload header type is 49.
        first, second = nal[0], nal[1]
        layer_id = ((first & 0x01) << 5) | (second >> 3)
        tid = second & 0x07
        nal_type = (first >> 1) & 0x3F

        # Payload header: F(1) type(6) layerId_hi(1) | layerId_lo(5) tid(3)
        fu_indicator = bytes(
            [
                (first & 0x80) | ((49 << 1) & 0x7E) | ((layer_id >> 5) & 0x01),
                ((layer_id & 0x1F) << 3) | tid,
            ]
        )

        body = nal[2:]
        chunk_size = limit - 3  # 2-byte payload header + 1-byte FU header
        out = []
        offset = 0
        while offset < len(body):
            chunk = body[offset : offset + chunk_size]
            start = offset == 0
            end = offset + len(chunk) >= len(body)
            fu_header = bytes(
                [(0x80 if start else 0) | (0x40 if end else 0) | nal_type]
            )
            out.append(fu_indicator + fu_header + chunk)
            offset += len(chunk)
        return out

    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #

    def packetize_aac(self, frames, timestamp):
        """Packetise AAC access units per RFC 3640 (mode=AAC-hbr).

        Each packet carries a 2-byte AU-headers-length field followed by one
        16-bit AU header holding the access unit size (13 bits) and index
        (3 bits). At 16 kHz mono an access unit is a few hundred bytes, so
        fragmentation is a defensive path rather than the norm.
        """
        packets = []
        limit = self._max_payload() - 4  # AU-headers-length + one AU header
        for unit in frames:
            if not unit:
                continue
            au_headers = struct.pack("!HH", 16, (len(unit) << 3) & 0xFFFF)
            if len(unit) <= limit:
                packets.append(self._emit(au_headers + unit, True, timestamp))
                continue
            offset = 0
            while offset < len(unit):
                chunk = unit[offset : offset + limit]
                offset += len(chunk)
                last = offset >= len(unit)
                packets.append(self._emit(au_headers + chunk, last, timestamp))
        return packets

    def sender_report(self, ntp_seconds, ntp_fraction, timestamp):
        """Build an RTCP sender report so clients can keep their clocks sane."""
        return struct.pack(
            "!BBHIIIII",
            (RTP_VERSION << 6),
            200,  # SR
            6,  # length in 32-bit words minus one
            self.ssrc,
            ntp_seconds,
            ntp_fraction,
            timestamp & 0xFFFFFFFF,
            self.packet_count & 0xFFFFFFFF,
        ) + struct.pack("!I", self.octet_count & 0xFFFFFFFF)
