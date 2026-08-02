"""Offline tests for the protocol layers - no camera required.

Run with:  python -m pytest tests/   (or plain: python tests/test_protocol.py)
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reolink2rtsp import baichuan, crypto  # noqa: E402
from reolink2rtsp.bcmedia import BcMediaParser, StreamInfo, VideoFrame  # noqa: E402
from reolink2rtsp.h26x import H264, H265, ParameterSets, split_nals  # noqa: E402
from reolink2rtsp.rtp import RTP_HEADER_LEN, RtpPacketizer  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers to synthesise a BcMedia stream
# --------------------------------------------------------------------------- #


def _pad(size):
    return b"\x00" * (0 if size % 8 == 0 else 8 - size % 8)


def make_info(width=2560, height=1440, fps=30):
    body = struct.pack("<4sIII", b"1001", 32, width, height)
    body += bytes([0, fps, 121, 8, 4, 23, 23, 52, 121, 8, 4, 23, 23, 52])
    body += b"\x00\x00"
    assert len(body) == 32, len(body)
    return body


def make_video(payload, keyframe=True, codec=b"H264", micros=1234, timestamp=99):
    magic = b"00dc" if keyframe else b"01dc"
    extra = struct.pack("<I", timestamp)  # additional_header_size == 4
    head = struct.pack(
        "<4s4sIII", magic, codec, len(payload), len(extra), micros
    ) + struct.pack("<I", 0) + extra
    assert len(head) == 24 + len(extra)
    return head + payload + _pad(len(payload))


def annexb(*nals):
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


# --------------------------------------------------------------------------- #


def test_crypto():
    crypto._self_test()

    # BC XOR is its own inverse at any offset.
    for offset in (0, 1, 250, 255):
        blob = bytes(range(256))
        assert crypto.bc_crypt(crypto.bc_crypt(blob, offset), offset) == blob

    # Known-answer for the truncated MD5 the protocol uses.
    assert len(crypto.md5_str_modern("anything")) == 31
    assert crypto.md5_str_modern("abc") == "900150983CD24FB0D6963F7D28E17F7"

    key = crypto.derive_aes_key("nonce123", "hunter2")
    assert len(key) == 16
    print("  crypto OK (backend: {})".format(crypto.AES_BACKEND))


def test_header_roundtrip():
    client = baichuan.BaichuanClient("127.0.0.1", "admin", "pw")

    data, msg_num = client._build(
        baichuan.MSG_VIDEO, b"BODY", channel_id=0, stream_type=1,
        msg_class=baichuan.CLASS_MODERN, payload_offset=0,
    )
    assert len(data) == 24 + 4
    assert data[0:4] == baichuan.MAGIC_BYTES

    (msg_id, body_len, channel_id, stream_type, num, code, klass) = struct.unpack(
        "<IIBBHHH", data[4:20]
    )
    assert msg_id == baichuan.MSG_VIDEO
    assert body_len == 4
    assert channel_id == 0
    assert stream_type == 1
    assert num == msg_num
    assert code == 0
    assert klass == baichuan.CLASS_MODERN
    assert struct.unpack("<I", data[20:24])[0] == 0

    # Legacy nonce request: 20-byte header, no payload offset.
    data, _ = client._build(
        baichuan.MSG_LOGIN, b"", msg_class=baichuan.CLASS_LEGACY, legacy_tag=0xDC12
    )
    assert len(data) == 20
    assert data[16:18] == b"\x12\xdc"  # the tag reolink's client sends
    assert data[18:20] == b"\x14\x65"
    print("  baichuan header OK")


def test_bcmedia_parser():
    iframe_payload = annexb(
        bytes([0x67]) + b"\x42\x00\x1f" + b"SPSDATA",  # SPS
        bytes([0x68]) + b"PPSDATA",  # PPS
        bytes([0x65]) + b"IDR" * 40,  # IDR slice
    )
    pframe_payload = annexb(bytes([0x41]) + b"SLICE" * 7)

    stream = (
        make_info()
        + make_video(iframe_payload, keyframe=True)
        + make_video(pframe_payload, keyframe=False, micros=1234 + 33333)
    )

    # Feed the whole thing at once.
    frames = BcMediaParser().feed(stream)
    assert len(frames) == 3, frames
    assert isinstance(frames[0], StreamInfo)
    assert (frames[0].width, frames[0].height, frames[0].fps) == (2560, 1440, 30)
    assert isinstance(frames[1], VideoFrame) and frames[1].keyframe
    assert frames[1].data == iframe_payload
    assert frames[1].codec == "H264"
    assert isinstance(frames[2], VideoFrame) and not frames[2].keyframe
    assert frames[2].data == pframe_payload

    # Feed it byte-by-byte in awkward chunks - the parser must be resumable.
    for chunk_size in (1, 3, 7, 64, 1000):
        parser = BcMediaParser()
        collected = []
        for pos in range(0, len(stream), chunk_size):
            collected.extend(parser.feed(stream[pos : pos + chunk_size]))
        assert len(collected) == 3, (chunk_size, collected)
        assert collected[1].data == iframe_payload
        assert collected[2].data == pframe_payload

    # Every I-frame/P-frame magic digit must be recognised.
    for digit in b"0123456789":
        blob = bytearray(make_video(b"\x00\x00\x00\x01\x65XYZ", keyframe=True))
        blob[0] = digit
        got = BcMediaParser().feed(bytes(blob))
        assert len(got) == 1 and got[0].keyframe, digit

    # Garbage in front must be skipped, not fatal.
    noisy = b"\xde\xad\xbe\xef" * 3 + make_video(iframe_payload)
    got = BcMediaParser().feed(noisy)
    assert len(got) == 1 and got[0].data == iframe_payload
    print("  bcmedia parser OK")


def test_split_nals():
    nals = list(split_nals(annexb(b"\x67AAA", b"\x68BB", b"\x65CCCC")))
    assert nals == [b"\x67AAA", b"\x68BB", b"\x65CCCC"]

    # 3-byte start codes too, and mixed.
    mixed = b"\x00\x00\x01\x67AAA" + b"\x00\x00\x00\x01\x68BB"
    assert list(split_nals(mixed)) == [b"\x67AAA", b"\x68BB"]

    # No start code at all -> single NAL.
    assert list(split_nals(b"\x67RAW")) == [b"\x67RAW"]
    print("  NAL splitting OK")


def test_parameter_sets():
    params = ParameterSets(H264)
    assert not params.ready
    assert params.observe(b"\x67\x42\x00\x1f" + b"S" * 10)
    assert params.observe(b"\x68" + b"P" * 4)
    assert not params.observe(b"\x65slice")
    assert params.ready
    fmtp = params.fmtp(96)
    assert "packetization-mode=1" in fmtp
    assert "profile-level-id=42001f" in fmtp
    assert "sprop-parameter-sets=" in fmtp
    assert params.rtpmap() == "H264/90000"

    hevc = ParameterSets(H265)
    assert hevc.observe(bytes([32 << 1]) + b"VPS")
    assert hevc.observe(bytes([33 << 1]) + b"SPS")
    assert hevc.observe(bytes([34 << 1]) + b"PPS")
    assert hevc.ready
    assert "sprop-vps=" in hevc.fmtp(96)
    assert hevc.rtpmap() == "H265/90000"
    print("  parameter sets OK")


def test_rtp_h264():
    pack = RtpPacketizer(H264, mtu=200)

    # Small NAL -> exactly one packet, marker set, payload passed through.
    small = b"\x65" + b"A" * 50
    packets = pack.packetize([small], 900)
    assert len(packets) == 1
    assert packets[0][RTP_HEADER_LEN:] == small
    assert packets[0][1] & 0x80, "marker bit must be set on the last packet"
    assert struct.unpack("!I", packets[0][4:8])[0] == 900

    # Large NAL -> FU-A, reassembling must reproduce the original.
    big = b"\x65" + bytes(range(256)) * 4
    packets = pack.packetize([big], 1800)
    assert len(packets) > 1
    rebuilt = b""
    for index, packet in enumerate(packets):
        body = packet[RTP_HEADER_LEN:]
        indicator, fu_header = body[0], body[1]
        assert indicator & 0x1F == 28, "FU-A type"
        assert indicator & 0xE0 == big[0] & 0xE0, "F/NRI preserved"
        assert fu_header & 0x1F == big[0] & 0x1F, "original NAL type preserved"
        assert bool(fu_header & 0x80) == (index == 0), "start bit"
        assert bool(fu_header & 0x40) == (index == len(packets) - 1), "end bit"
        assert len(packet) <= 200
        rebuilt += body[2:]
    assert bytes([big[0]]) + rebuilt == big

    # Only the final packet of an access unit carries the marker.
    packets = pack.packetize([b"\x67SPS", b"\x68PPS", b"\x65" + b"Z" * 30], 2700)
    assert len(packets) == 3
    assert [bool(p[1] & 0x80) for p in packets] == [False, False, True]

    # Sequence numbers increment by one and wrap cleanly.
    pack.sequence = 0xFFFE
    packets = pack.packetize([b"\x65a", b"\x65b", b"\x65c"], 10)
    seqs = [struct.unpack("!H", p[2:4])[0] for p in packets]
    assert seqs == [0xFFFE, 0xFFFF, 0x0000]
    print("  RTP H.264 OK")


def test_rtp_h265():
    pack = RtpPacketizer(H265, mtu=120)
    header = bytes([(19 << 1), 0x01])  # IDR_W_RADL, layer 0, tid 1
    big = header + bytes(range(256)) * 2

    packets = pack.packetize([big], 3600)
    assert len(packets) > 1
    rebuilt = b""
    for index, packet in enumerate(packets):
        body = packet[RTP_HEADER_LEN:]
        assert (body[0] >> 1) & 0x3F == 49, "FU payload header type"
        assert body[1] & 0x07 == 1, "temporal id preserved"
        fu_header = body[2]
        assert fu_header & 0x3F == 19, "original NAL type preserved"
        assert bool(fu_header & 0x80) == (index == 0)
        assert bool(fu_header & 0x40) == (index == len(packets) - 1)
        assert len(packet) <= 120
        rebuilt += body[3:]
    assert header + rebuilt == big

    small = header + b"tiny"
    packets = pack.packetize([small], 3600)
    assert len(packets) == 1 and packets[0][RTP_HEADER_LEN:] == small
    print("  RTP H.265 OK")


class _BitWriter(object):
    """Minimal bit writer so we can synthesise a valid SPS to parse back."""

    def __init__(self):
        self.bits = []

    def u(self, value, count):
        for shift in range(count - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def ue(self, value):
        value += 1
        length = value.bit_length()
        self.u(0, length - 1)
        self.u(value, length)

    def bytes(self):
        self.bits.append(1)  # rbsp_stop_one_bit
        while len(self.bits) % 8:
            self.bits.append(0)
        out = bytearray()
        for pos in range(0, len(self.bits), 8):
            byte = 0
            for bit in self.bits[pos : pos + 8]:
                byte = (byte << 1) | bit
            out.append(byte)
        return bytes(out)


def _make_sps(width_mbs, height_map_units, crop_bottom=0, level=40):
    writer = _BitWriter()
    writer.u(66, 8)  # profile_idc = baseline (skips the scaling-matrix branch)
    writer.u(0, 8)  # constraint flags + reserved
    writer.u(level, 8)  # level_idc
    writer.ue(0)  # seq_parameter_set_id
    writer.ue(0)  # log2_max_frame_num_minus4
    writer.ue(2)  # pic_order_cnt_type = 2
    writer.ue(1)  # max_num_ref_frames
    writer.u(0, 1)  # gaps_in_frame_num_value_allowed_flag
    writer.ue(width_mbs - 1)  # pic_width_in_mbs_minus1
    writer.ue(height_map_units - 1)  # pic_height_in_map_units_minus1
    writer.u(1, 1)  # frame_mbs_only_flag
    writer.u(1, 1)  # direct_8x8_inference_flag
    if crop_bottom:
        writer.u(1, 1)  # frame_cropping_flag
        writer.ue(0)  # crop_left
        writer.ue(0)  # crop_right
        writer.ue(0)  # crop_top
        writer.ue(crop_bottom)
    else:
        writer.u(0, 1)
    writer.u(0, 1)  # vui_parameters_present_flag
    return bytes([0x67]) + writer.bytes()


def test_h264_resolution():
    # 1920x1080: 120 macroblocks wide, 68 map units tall (=1088) cropped by 4*2.
    params = ParameterSets(H264)
    params.observe(_make_sps(120, 68, crop_bottom=4))
    assert params.resolution() == (1920, 1080), params.resolution()

    # 2560x1440 needs no cropping: 160 x 90 macroblocks.
    params = ParameterSets(H264)
    params.observe(_make_sps(160, 90))
    assert params.resolution() == (2560, 1440), params.resolution()

    # 640x360, cropped from 640x368.
    params = ParameterSets(H264)
    params.observe(_make_sps(40, 23, crop_bottom=4))
    assert params.resolution() == (640, 360), params.resolution()

    # A malformed SPS must return None rather than raising.
    params = ParameterSets(H264)
    params.observe(b"\x67\xff")
    assert params.resolution() is None
    print("  SPS resolution parsing OK")


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    print("running {} test groups\n".format(len(tests)))
    for test in tests:
        test()
    print("\nall protocol tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
