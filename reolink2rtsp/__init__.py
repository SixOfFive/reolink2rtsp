"""reolink2rtsp - an RTSP bridge for Reolink cameras that have no RTSP.

Some Reolink models (the Lumus E430 among them) ship with no RTSP, ONVIF, RTMP
or HTTP service at all: TCP port 9000 and their proprietary "Baichuan" protocol
is the only way in. This package speaks that protocol, pulls the camera's native
H.264/H.265 stream, and republishes it as standard RTSP for Frigate, go2rtc,
VLC, or anything else that speaks RTSP.

No transcoding, no ffmpeg, no external RTSP server - the whole path is Python
standard library.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
