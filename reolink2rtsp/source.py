"""Camera source: Baichuan connection -> decoded access units -> subscribers.

One :class:`CameraSource` per configured camera. It connects lazily (when the
first RTSP client asks for it), keeps itself alive with pings, reconnects with
backoff, and drops the camera connection again once nobody is watching.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .baichuan import BaichuanClient, BaichuanError, LoginFailed
from .bcmedia import BcMediaParser, StreamInfo, VideoFrame
from .h26x import H264, H265, ParameterSets, split_nals, nal_type
from .h26x import H264_NAL_AUD, H264_NAL_PPS, H264_NAL_SPS
from .h26x import H265_NAL_AUD, H265_NAL_PPS, H265_NAL_SPS, H265_NAL_VPS

_LOG = logging.getLogger(__name__)

CLOCK_RATE = 90000
MAX_SANE_GAP_US = 10_000_000  # 10s; anything larger means the camera clock jumped


class AccessUnit(object):
    """One decodable picture, as a list of NAL units."""

    __slots__ = ("nals", "keyframe", "timestamp", "wallclock")

    def __init__(self, nals, keyframe, timestamp, wallclock):
        self.nals = nals
        self.keyframe = keyframe
        self.timestamp = timestamp  # 90 kHz RTP timestamp
        self.wallclock = wallclock


class Subscriber(object):
    """A queue of access units for one RTSP session."""

    def __init__(self, maxsize=120):
        self.queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.started = False  # set once we have sent a keyframe

    def offer(self, unit):
        # Wait for a keyframe before sending anything, or the client sees
        # garbage until the next IDR.
        if not self.started:
            if not unit.keyframe:
                return
            self.started = True
        try:
            self.queue.put_nowait(unit)
        except asyncio.QueueFull:
            # Slow client: drop everything and resync on the next keyframe.
            self.dropped += 1
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self.started = False


class CameraSource(object):
    def __init__(self, config):
        self.config = config
        self.name = config.name
        self.codec = None
        self.params = None
        self.info = None

        self._subscribers = set()
        self._task = None
        self._client = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()

        self._rtp_timestamp = 0
        self._last_us = None
        self._last_wall = None

        self.connected = False
        self.last_error = None
        self.frames_seen = 0
        self.bytes_seen = 0
        self.started_at = None

    # ------------------------------------------------------------------ #
    # Subscriber management
    # ------------------------------------------------------------------ #

    def subscribe(self):
        sub = Subscriber(maxsize=self.config.queue_size)
        self._subscribers.add(sub)
        _LOG.info("%s: client attached (%d total)", self.name, len(self._subscribers))
        self._ensure_running()
        return sub

    def unsubscribe(self, sub):
        self._subscribers.discard(sub)
        _LOG.info("%s: client detached (%d left)", self.name, len(self._subscribers))

    @property
    def subscriber_count(self):
        return len(self._subscribers)

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.ensure_future(self._run())

    async def wait_ready(self, timeout):
        """Block until parameter sets are known (needed to answer DESCRIBE)."""
        self._ensure_running()
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def shutdown(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except LoginFailed as exc:
                self.last_error = str(exc)
                _LOG.error("%s: %s - check credentials, not retrying fast", self.name, exc)
                backoff = 60.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                _LOG.warning("%s: %s", self.name, exc)
            finally:
                self.connected = False

            if self._stop.is_set() or not self._subscribers:
                if not self.config.always_on:
                    _LOG.debug("%s: no subscribers, idling", self.name)
                    return

            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _session(self):
        cfg = self.config
        client = BaichuanClient(
            cfg.host, cfg.username, cfg.password, port=cfg.port, timeout=cfg.timeout
        )
        self._client = client
        stream_num = None
        try:
            await client.login()
            self.connected = True
            self.started_at = time.time()
            stream_num, queue = await client.start_video(cfg.stream, cfg.channel)

            parser = BcMediaParser(on_desync=self._on_desync)
            ping_at = time.monotonic() + cfg.ping_interval
            idle_since = None

            while not self._stop.is_set():
                timeout = max(0.5, ping_at - time.monotonic())
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout)
                except asyncio.TimeoutError:
                    await client.ping()
                    ping_at = time.monotonic() + cfg.ping_interval
                    continue

                if chunk is None:
                    raise BaichuanError("{}: camera closed the stream".format(self.name))

                self.bytes_seen += len(chunk)
                for frame in parser.feed(chunk):
                    if isinstance(frame, StreamInfo):
                        self.info = frame
                        _LOG.info("%s: %r", self.name, frame)
                    elif isinstance(frame, VideoFrame):
                        self._on_video(frame)

                # Drop the camera connection when nobody is watching.
                if not self._subscribers and not cfg.always_on:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > cfg.idle_timeout:
                        _LOG.info("%s: idle, disconnecting", self.name)
                        return
                else:
                    idle_since = None

                if time.monotonic() >= ping_at:
                    await client.ping()
                    ping_at = time.monotonic() + cfg.ping_interval
        finally:
            self.connected = False
            if stream_num is not None:
                try:
                    await client.stop_video(stream_num, cfg.stream, cfg.channel)
                except Exception:
                    pass
            await client.close()
            self._client = None

    def _on_desync(self, head):
        _LOG.debug("%s: bcmedia desync at %s", self.name, head.hex())

    # ------------------------------------------------------------------ #
    # Frame handling
    # ------------------------------------------------------------------ #

    def _advance_clock(self, frame):
        """Advance the 90 kHz RTP clock, preferring the camera's own timestamps."""
        now = time.monotonic()
        micros = frame.microseconds

        if self._last_us is None:
            self._last_us = micros
            self._last_wall = now
            return self._rtp_timestamp

        delta_us = (micros - self._last_us) & 0xFFFFFFFF
        if delta_us == 0 or delta_us > MAX_SANE_GAP_US:
            # Camera clock jumped or wrapped oddly - fall back to wall time.
            delta_us = max(0, int((now - self._last_wall) * 1_000_000))
            if delta_us > MAX_SANE_GAP_US:
                delta_us = 0

        self._last_us = micros
        self._last_wall = now
        self._rtp_timestamp = (
            self._rtp_timestamp + (delta_us * CLOCK_RATE) // 1_000_000
        ) & 0xFFFFFFFF
        return self._rtp_timestamp

    def _on_video(self, frame):
        codec = frame.codec
        if self.codec != codec:
            self.codec = codec
            self.params = ParameterSets(codec)
            _LOG.info("%s: codec is %s", self.name, codec)

        nals = []
        has_params = False
        aud_type = H265_NAL_AUD if codec == H265 else H264_NAL_AUD
        for nal in split_nals(frame.data):
            ntype = nal_type(nal, codec)
            if ntype == aud_type:
                continue  # access unit delimiters add nothing over RTP
            if self.params.observe(nal):
                has_params = True
            nals.append(nal)

        if not nals:
            return

        # A keyframe must be self-contained: if the camera did not inline the
        # parameter sets, put them in front.
        if frame.keyframe and not has_params and self.params.ready:
            nals = self.params.prefix_nals() + nals

        timestamp = self._advance_clock(frame)
        self.frames_seen += 1

        if self.params.ready and not self._ready.is_set():
            self._ready.set()
            resolution = self.params.resolution()
            _LOG.info(
                "%s: ready%s",
                self.name,
                " ({}x{})".format(*resolution) if resolution else "",
            )

        unit = AccessUnit(nals, frame.keyframe, timestamp, time.time())
        for sub in list(self._subscribers):
            sub.offer(unit)

    # ------------------------------------------------------------------ #

    def status(self):
        return {
            "name": self.name,
            "host": self.config.host,
            "stream": self.config.stream,
            "connected": self.connected,
            "codec": self.codec,
            "clients": len(self._subscribers),
            "frames": self.frames_seen,
            "bytes": self.bytes_seen,
            "resolution": (
                "{}x{}".format(self.info.width, self.info.height) if self.info else None
            ),
            "fps": self.info.fps if self.info else None,
            "error": self.last_error,
        }
