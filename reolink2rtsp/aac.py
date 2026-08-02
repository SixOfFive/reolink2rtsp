"""AAC helpers: ADTS parsing and the AudioSpecificConfig an SDP needs.

The camera sends AAC-LC wrapped in ADTS, which is convenient - the sample rate,
channel count and profile are all self-describing, so the ``config=`` string in
the SDP can be derived from the stream itself rather than guessed or configured.

RTP wants the raw AAC access unit without the ADTS wrapper, so this module also
strips it.
"""

from __future__ import annotations

__all__ = ["AdtsInfo", "parse_adts", "strip_adts", "audio_specific_config",
           "SAMPLE_RATES", "SAMPLES_PER_FRAME"]

# ISO/IEC 14496-3 sampling frequency table, indexed by the ADTS field.
SAMPLE_RATES = (
    96000, 88200, 64000, 48000, 44100, 32000,
    24000, 22050, 16000, 12000, 11025, 8000, 7350,
)

# One AAC-LC access unit is always 1024 samples, which is what the RTP
# timestamp advances by per frame.
SAMPLES_PER_FRAME = 1024


class AdtsInfo(object):
    __slots__ = ("object_type", "rate_index", "sample_rate", "channels",
                 "frame_length", "header_length")

    def __init__(self, object_type, rate_index, sample_rate, channels,
                 frame_length, header_length):
        self.object_type = object_type  # 2 = AAC-LC
        self.rate_index = rate_index
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_length = frame_length  # including the ADTS header
        self.header_length = header_length  # 7, or 9 when a CRC is present

    def __repr__(self):
        return "<AAC obj={} {}Hz ch={}>".format(
            self.object_type, self.sample_rate, self.channels
        )


def parse_adts(data):
    """Parse an ADTS header. Returns None if *data* is not ADTS."""
    if len(data) < 7:
        return None
    if data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        return None

    protection_absent = data[1] & 0x01
    object_type = ((data[2] >> 6) & 0x03) + 1
    rate_index = (data[2] >> 2) & 0x0F
    channels = ((data[2] & 0x01) << 2) | ((data[3] >> 6) & 0x03)
    frame_length = ((data[3] & 0x03) << 11) | (data[4] << 3) | ((data[5] >> 5) & 0x07)

    if rate_index >= len(SAMPLE_RATES):
        return None

    return AdtsInfo(
        object_type=object_type,
        rate_index=rate_index,
        sample_rate=SAMPLE_RATES[rate_index],
        channels=channels,
        frame_length=frame_length,
        header_length=7 if protection_absent else 9,
    )


def strip_adts(data, info=None):
    """Return the raw AAC access unit, without the ADTS header."""
    if info is None:
        info = parse_adts(data)
    if info is None:
        return data
    end = info.frame_length if 0 < info.frame_length <= len(data) else len(data)
    return data[info.header_length : end]


def audio_specific_config(info):
    """Build the 2-byte AudioSpecificConfig, hex-encoded for the SDP.

    Layout: audioObjectType(5) samplingFrequencyIndex(4) channelConfiguration(4)
    frameLengthFlag(1) dependsOnCoreCoder(1) extensionFlag(1).

    AAC-LC 16 kHz mono comes out as ``1408``.
    """
    bits = (
        (info.object_type & 0x1F) << 11
        | (info.rate_index & 0x0F) << 7
        | (info.channels & 0x0F) << 3
    )
    return "{:04x}".format(bits)
