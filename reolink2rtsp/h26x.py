"""Annex-B bitstream helpers for H.264 and H.265.

Just enough to split NAL units, recognise parameter sets, and build the SDP
``fmtp`` line an RTSP client needs before it can decode anything.
"""

from __future__ import annotations

import base64

__all__ = [
    "split_nals",
    "ParameterSets",
    "H264",
    "H265",
]

H264 = "H264"
H265 = "H265"

# H.264 nal_unit_type values
H264_NAL_NON_IDR = 1
H264_NAL_IDR = 5
H264_NAL_SEI = 6
H264_NAL_SPS = 7
H264_NAL_PPS = 8
H264_NAL_AUD = 9

# H.265 nal_unit_type values
H265_NAL_VPS = 32
H265_NAL_SPS = 33
H265_NAL_PPS = 34
H265_NAL_AUD = 35


def split_nals(data):
    """Yield NAL units (without start codes) from an Annex-B buffer."""
    length = len(data)
    idx = 0
    starts = []
    while idx < length - 2:
        if data[idx] == 0 and data[idx + 1] == 0:
            if data[idx + 2] == 1:
                starts.append((idx, idx + 3))
                idx += 3
                continue
            if (
                idx < length - 3
                and data[idx + 2] == 0
                and data[idx + 3] == 1
            ):
                starts.append((idx, idx + 4))
                idx += 4
                continue
        idx += 1

    if not starts:
        # No start codes at all - treat the whole buffer as one NAL.
        if data:
            yield bytes(data)
        return

    for pos, (start_marker, body) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else length
        nal = data[body:end]
        if nal:
            yield bytes(nal)


def nal_type(nal, codec):
    if not nal:
        return -1
    if codec == H265:
        return (nal[0] >> 1) & 0x3F
    return nal[0] & 0x1F


def is_keyframe_nal(nal, codec):
    ntype = nal_type(nal, codec)
    if codec == H265:
        return 16 <= ntype <= 23  # BLA/IDR/CRA
    return ntype == H264_NAL_IDR


class ParameterSets(object):
    """Collects VPS/SPS/PPS as they appear and renders SDP for them."""

    def __init__(self, codec):
        self.codec = codec
        self.vps = None
        self.sps = None
        self.pps = None

    @property
    def ready(self):
        if self.codec == H265:
            return bool(self.vps and self.sps and self.pps)
        return bool(self.sps and self.pps)

    def observe(self, nal):
        """Record *nal* if it is a parameter set. Returns True if it was."""
        ntype = nal_type(nal, self.codec)
        if self.codec == H265:
            if ntype == H265_NAL_VPS:
                self.vps = nal
                return True
            if ntype == H265_NAL_SPS:
                self.sps = nal
                return True
            if ntype == H265_NAL_PPS:
                self.pps = nal
                return True
            return False

        if ntype == H264_NAL_SPS:
            self.sps = nal
            return True
        if ntype == H264_NAL_PPS:
            self.pps = nal
            return True
        return False

    def prefix_nals(self):
        """Parameter sets to prepend to every keyframe, in decode order."""
        if self.codec == H265:
            return [n for n in (self.vps, self.sps, self.pps) if n]
        return [n for n in (self.sps, self.pps) if n]

    # ------------------------------------------------------------------ #

    def rtpmap(self):
        return "H265/90000" if self.codec == H265 else "H264/90000"

    def fmtp(self, payload_type):
        if self.codec == H265:
            parts = ["packetization-mode=1"]
            if self.vps:
                parts.append("sprop-vps=" + _b64(self.vps))
            if self.sps:
                parts.append("sprop-sps=" + _b64(self.sps))
            if self.pps:
                parts.append("sprop-pps=" + _b64(self.pps))
            return "a=fmtp:{} {}".format(payload_type, "; ".join(parts))

        profile = "42001f"
        if self.sps and len(self.sps) >= 4:
            profile = self.sps[1:4].hex()
        sprop = ""
        if self.sps and self.pps:
            sprop = "; sprop-parameter-sets={},{}".format(_b64(self.sps), _b64(self.pps))
        return "a=fmtp:{} packetization-mode=1; profile-level-id={}{}".format(
            payload_type, profile, sprop
        )

    def resolution(self):
        """Best-effort width/height from the SPS. Returns None if unparsed."""
        if self.codec != H264 or not self.sps:
            return None
        try:
            return _h264_resolution(self.sps)
        except Exception:
            return None


def _b64(data):
    return base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- #
# Minimal H.264 SPS parser - only used for logging, never for correctness.
# --------------------------------------------------------------------------- #


class _BitReader(object):
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def bit(self):
        byte = self.data[self.pos >> 3]
        value = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("bad exp-golomb")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self):
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _unescape(data):
    """Strip emulation-prevention bytes."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _h264_resolution(sps):
    reader = _BitReader(_unescape(sps[1:]))
    profile_idc = reader.bits(8)
    reader.bits(8)  # constraint flags + reserved
    reader.bits(8)  # level_idc
    reader.ue()  # seq_parameter_set_id

    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = reader.ue()
        if chroma_format_idc == 3:
            reader.bit()  # separate_colour_plane_flag
        reader.ue()  # bit_depth_luma_minus8
        reader.ue()  # bit_depth_chroma_minus8
        reader.bit()  # qpprime_y_zero_transform_bypass_flag
        if reader.bit():  # seq_scaling_matrix_present_flag
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                if reader.bit():
                    size = 16 if i < 6 else 64
                    last = next_scale = 8
                    for _ in range(size):
                        if next_scale != 0:
                            next_scale = (last + reader.se() + 256) % 256
                        last = next_scale if next_scale != 0 else last

    reader.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = reader.ue()
    if pic_order_cnt_type == 0:
        reader.ue()
    elif pic_order_cnt_type == 1:
        reader.bit()
        reader.se()
        reader.se()
        for _ in range(reader.ue()):
            reader.se()

    reader.ue()  # max_num_ref_frames
    reader.bit()  # gaps_in_frame_num_value_allowed_flag
    width_mbs = reader.ue() + 1
    height_map = reader.ue() + 1
    frame_mbs_only = reader.bit()
    if not frame_mbs_only:
        reader.bit()  # mb_adaptive_frame_field_flag
    reader.bit()  # direct_8x8_inference_flag

    crop_left = crop_right = crop_top = crop_bottom = 0
    if reader.bit():  # frame_cropping_flag
        crop_left = reader.ue()
        crop_right = reader.ue()
        crop_top = reader.ue()
        crop_bottom = reader.ue()

    width = width_mbs * 16
    height = (2 - frame_mbs_only) * height_map * 16

    sub_w = 2 if chroma_format_idc in (1, 2) else 1
    sub_h = 2 if chroma_format_idc == 1 else 1
    width -= (crop_left + crop_right) * sub_w
    height -= (crop_top + crop_bottom) * sub_h * (2 - frame_mbs_only)
    return width, height
