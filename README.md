# reolink2rtsp

An RTSP bridge for Reolink cameras that **have no RTSP**.

Some Reolink models — the Lumus E430 among them — ship with no RTSP, no ONVIF,
no RTMP and no HTTP API. Port scan one and you get a single open port:

```
192.168.15.60   closed 80  closed 443  closed 554  closed 8000   OPEN 9000
```

TCP 9000 is Reolink's proprietary **Baichuan** protocol, the one their phone app
and NVRs speak. This project talks that protocol directly, pulls the camera's
native H.264/H.265 stream, and republishes it as ordinary RTSP for
[Frigate](https://frigate.video/), go2rtc, VLC, or anything else.

**No transcoding.** Frames come off the camera already encoded and are passed
through untouched — only re-packetised into RTP. CPU cost is negligible.

## Why not just use ffmpeg?

You can't. ffmpeg has no Baichuan support, and there is no RTSP endpoint on the
camera for it to read. The protocol has to be spoken natively first — that is
what this does.

## Tested hardware

Developed and verified against five **Reolink Lumus** cameras on one LAN.

| | |
|---|---|
| Model | Reolink Lumus, model id **E430** |
| Hardware revision | `IPC_NT1NO24MP` |
| Firmware | **`v3.1.0.4713_2503191383`** (Reolink versioning) |
| Firmware (Baichuan `firmVersion`) | `00326417580108`, `softVer` `50397655` |
| Device type | `wifi_solo_ipc` / `IPC`, 1 channel, 1 audio input, SD card slot |
| Open ports | **TCP 9000 only** — no 80, 443, 554, 1935 or 8000 |
| PTZ | none |

What those cameras actually stream:

| Stream | Codec | Resolution | Rate | Bitrate |
|---|---|---|---|---|
| `main` | H.264 High (`profile-level-id=640033`) | 2560×1440 | ~15 fps | ~3.2 Mbit/s |
| `sub` | H.264 | 640×360 | ~10 fps | ~0.3 Mbit/s |
| audio | AAC-LC, ADTS framed | mono | 16 kHz | ~50 kbit/s |

`extern` is accepted by the config but this model does not provide it.

Nothing above is hardcoded — resolution, frame rate and audio format are all
discovered at runtime from the stream itself (the `1001`/`1002` info block, the
H.264 SPS, and the AAC ADTS headers). A different Baichuan model with different
resolutions should work without changes; see [Other models](#other-models).

## Requirements

* Python 3.8+
* **No mandatory third-party packages.** The protocol, AES, RTP packetisation
  and RTSP server are all standard library. If `cryptography` or `pycryptodome`
  happens to be installed it is used to speed up AES, but neither is required.

Installing `cryptography` is nonetheless recommended for anything beyond a
quick test: the camera encrypts part of every video frame, so AES sits in the
media path and the pure-Python fallback is much slower.

## Quick start

```bash
git clone https://github.com/SixOfFive/reolink2rtsp
cd reolink2rtsp
cp reolink2rtsp.ini.example reolink2rtsp.ini
```

Put your camera password in the environment (never in the config file):

```bash
export REOLINK_PASSWORD='your-camera-password'
```

Check a camera answers before wiring anything up:

```bash
python -m reolink2rtsp probe 192.168.15.60 --password "$REOLINK_PASSWORD"
```

Then run the server:

```bash
python -m reolink2rtsp serve -c reolink2rtsp.ini
```

Streams are now live, each camera on its own port, video plus audio:

```
rtsp://test:test@<host>:8554/driveway
rtsp://test:test@<host>:8555/living_room
rtsp://test:test@<host>:8556/work_area
```

Verify with ffprobe:

```bash
ffprobe -rtsp_transport tcp rtsp://test:test@127.0.0.1:8554/driveway
```

## Configuration

INI format, one `[camera:<name>]` section per camera. Each camera gets its own
RTSP port and its own set of RTSP logins, so you can hand different credentials
to different consumers.

```ini
[server]
bind      = 0.0.0.0
base_port = 8554       ; cameras without an explicit rtsp_port count up from here
mtu       = 1400

[defaults]
users = test:test      ; applied to any camera that doesn't list its own

[camera:driveway]
host      = 192.168.15.60
username  = admin
password  = ${REOLINK_PASSWORD}   ; never a literal
stream    = main                  ; main | sub | extern
rtsp_port = 8554
users     = test:test, frigate:somethingelse
```

### Credentials

There are two independent sets and it matters which is which:

| | What it is | Where it lives |
|---|---|---|
| **Camera** `username`/`password` | The Reolink login, used to authenticate *to* the camera on port 9000 | `${ENV_VAR}` only — real values must never be committed |
| **RTSP** `users` | Logins clients use to pull the stream *from* this bridge | Fine to keep in the config; `test:test` by default |

`reolink2rtsp.ini` is gitignored for exactly this reason. Only
`reolink2rtsp.ini.example`, which contains no camera secrets, is committed.

Any value supports `${VAR}` and `${VAR:-fallback}`.

### Ports below 1024

The default `base_port` is **8554**, which needs no privileges anywhere. 554 is
the IANA-registered RTSP port (clients can then omit it from the URL) but on
Linux/macOS anything below 1024 requires root. To use it, grant the capability
once:

```bash
sudo setcap 'cap_net_bind_service=+ep' "$(readlink -f "$(which python3)")"
```

Windows has no such restriction. If the bind fails, reolink2rtsp says so
explicitly rather than printing a traceback.

## Frigate

```yaml
cameras:
  driveway:
    ffmpeg:
      inputs:
        - path: rtsp://test:test@192.168.15.3:8554/driveway
          input_args: preset-rtsp-restream
          roles: [detect, record]
```

## Audio

The camera's AAC audio is served as a second RTSP track and is on by default.
Clients that want it `SETUP` `trackID=1` after the video track; clients that
ignore it get video only, so nothing breaks either way.

```ini
[camera:driveway]
audio = false      ; or --no-audio on the command line
```

Audio format is read from the stream's own ADTS headers, so the SDP `config=`
is derived rather than assumed:

```
m=audio 0 RTP/AVP 97
a=rtpmap:97 mpeg4-generic/16000/1
a=fmtp:97 streamtype=5; profile-level-id=1; mode=AAC-hbr; sizelength=13; indexlength=3; indexdeltalength=3; config=1408
a=control:trackID=1
```

Packetisation follows RFC 3640 (`mode=AAC-hbr`), one access unit per packet,
with the RTP clock stepping 1024 samples per frame.

## How it works

```
TCP 9000
   │  legacy header-only message  ──►  camera returns a nonce
   │  MD5(user+nonce) / MD5(pass+nonce)  ──►  login
   │  AES-128-CFB session key = MD5(nonce + "-" + pass)[:16]
   │  Preview command  ──►  camera starts pushing media
   ▼
BcMedia chunks  ("1001" info, "N0dc" I-frame, "N1dc" P-frame, "05wb" AAC)
   ▼                                          8-byte aligned
H.264/H.265 Annex-B NAL units          AAC access units (ADTS stripped)
   │  SPS/PPS/VPS captured for the SDP        │
   ▼                                          ▼
RTP  single-NAL or FU-A/FU, 90 kHz     RTP  RFC 3640, 16 kHz
   ▼                                          ▼
RTSP   trackID=0                              trackID=1
       (TCP interleaved or UDP; Basic and Digest auth)
```

### Media encryption, which is not what it looks like

XML message bodies are AES-128-CFB encrypted, so the obvious assumption is that
media is too. It is not, quite: **only the message that *starts* a video frame
is encrypted, and the rest of that frame follows in the clear**, as do the
32-byte stream-info blocks.

Decrypting every media message therefore corrupts everything past the first
~1 KB of each frame. That failure is easy to miss, because frame *lengths* come
from the header, so the parser reports zero desyncs and perfectly sensible
frame counts while the pixel data is ruined — the visible symptom is a correct
strip at the top of the picture, garbage below, and a freeze as every following
P-frame references the broken key frame.

Neither Neolink nor reolink_aio documents this, as far as I can tell.

Cameras connect on demand — the bridge only opens a socket to the camera while
something is actually watching, then disconnects after `idle_timeout`. Set
`always_on = true` to keep a camera connected permanently.

### Module layout

| Module | Responsibility |
|---|---|
| `crypto.py` | BC XOR, the protocol's truncated MD5, AES-128-CFB (+ pure-Python fallback) |
| `baichuan.py` | Wire framing, login, command/response, stream start, media decryption |
| `bcmedia.py` | Resumable parser for the BcMedia container |
| `h26x.py` | NAL splitting, parameter sets, SDP fmtp, SPS resolution |
| `aac.py` | ADTS parsing and the AudioSpecificConfig for the SDP |
| `rtp.py` | RTP packetisation for H.264, H.265 and AAC |
| `rtsp.py` | RTSP server, multi-track sessions, transports, auth |
| `source.py` | Per-camera pipeline, reconnect, fan-out to clients |
| `config.py` | INI loading with `${ENV}` expansion and CLI overrides |

### Other models

Resolution, frame rate, codec and audio format are all discovered at runtime, so
a different Baichuan camera should serve correctly without code changes. Known
limits:

* **`externStream` is not universal.** B800-class cameras have it; the Lumus
  does not. Selecting it where it is absent fails rather than falling back.
* **Multi-channel devices** (NVRs, dual-lens) need channel handling beyond
  channel 0.
* **H.265 is implemented but untested on real hardware** — the packetiser and
  SDP are covered by unit tests, but every camera here is H.264.

## Command line

Every configuration value can be overridden from the command line, and sections
are created on demand, so a camera can be defined entirely in argv:

```bash
python -m reolink2rtsp serve \
    -o camera:test.host=192.168.15.60 \
    -o camera:test.rtsp_port=8554 \
    --password "$REOLINK_PASSWORD" --stream sub
```

Shorthands: `--bind`, `--base-port`, `--mtu`, `--describe-timeout`,
`--username`, `--password`, `--stream`, `--users`, `--only`, `--always-on`,
`--audio` / `--no-audio`. A bare `-o KEY=VALUE` targets `[server]`.

## Testing

Offline tests need no camera and no network:

```bash
python tests/test_protocol.py
```

They cover the AES implementation against the FIPS-197 vector, header framing,
the BcMedia parser (including byte-at-a-time feeding and resync from garbage),
NAL splitting, SPS parsing, and RTP fragmentation round-trips.

## Credits

The Baichuan protocol was reverse-engineered by the
[Neolink](https://github.com/thirtythreeforty/neolink) project and its
[maintained fork](https://github.com/QuantumEntangledAndy/neolink); the login and
encryption details cross-check against
[reolink_aio](https://github.com/starkillerOG/reolink_aio), the library Home
Assistant uses. This is an independent pure-Python implementation.

## Licence

MIT
