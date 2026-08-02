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

## Requirements

* Python 3.8+
* **No mandatory third-party packages.** The protocol, AES, RTP packetisation
  and RTSP server are all standard library. If `cryptography` or `pycryptodome`
  happens to be installed it is used to speed up AES, but neither is required.

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

Streams are now live:

```
rtsp://test:test@<host>:554/driveway
rtsp://test:test@<host>:555/living_room
rtsp://test:test@<host>:556/work_area
```

Verify with ffprobe:

```bash
ffprobe -rtsp_transport tcp rtsp://test:test@127.0.0.1:554/driveway
```

## Configuration

INI format, one `[camera:<name>]` section per camera. Each camera gets its own
RTSP port and its own set of RTSP logins, so you can hand different credentials
to different consumers.

```ini
[server]
bind      = 0.0.0.0
base_port = 554        ; cameras without an explicit rtsp_port count up from here
mtu       = 1400

[defaults]
users = test:test      ; applied to any camera that doesn't list its own

[camera:driveway]
host      = 192.168.15.60
username  = admin
password  = ${REOLINK_PASSWORD}   ; never a literal
stream    = main                  ; main | sub | extern
rtsp_port = 554
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

`base_port = 554` is the standard RTSP port but needs privileges on Linux/macOS.
Either set `base_port = 8554`, or grant the capability once:

```bash
sudo setcap 'cap_net_bind_service=+ep' "$(readlink -f "$(which python3)")"
```

Windows has no such restriction.

## Frigate

```yaml
cameras:
  driveway:
    ffmpeg:
      inputs:
        - path: rtsp://test:test@192.168.15.3:554/driveway
          input_args: preset-rtsp-restream
          roles: [detect, record]
```

## How it works

```
TCP 9000
   │  legacy header-only message  ──►  camera returns a nonce
   │  MD5(user+nonce) / MD5(pass+nonce)  ──►  login
   │  AES-128-CFB session key = MD5(nonce + "-" + pass)[:16]
   │  Preview command  ──►  camera starts pushing media
   ▼
BcMedia chunks  ("1001" info, "N0dc" I-frame, "N1dc" P-frame, 8-byte aligned)
   ▼
H.264/H.265 Annex-B NAL units  (SPS/PPS/VPS captured for the SDP)
   ▼
RTP  (single-NAL or FU-A/FU fragmentation, 90 kHz clock)
   ▼
RTSP  (TCP interleaved or UDP; Basic and Digest auth)
```

Cameras connect on demand — the bridge only opens a socket to the camera while
something is actually watching, then disconnects after `idle_timeout`. Set
`always_on = true` to keep a camera connected permanently.

### Module layout

| Module | Responsibility |
|---|---|
| `crypto.py` | BC XOR, the protocol's truncated MD5, AES-128-CFB (+ pure-Python fallback) |
| `baichuan.py` | Wire framing, login, command/response, video stream start |
| `bcmedia.py` | Resumable parser for the BcMedia container |
| `h26x.py` | NAL splitting, parameter sets, SDP fmtp, SPS resolution |
| `rtp.py` | RTP packetisation for H.264 and H.265 |
| `rtsp.py` | RTSP server, session and transport handling |
| `source.py` | Per-camera pipeline, reconnect, fan-out to clients |
| `config.py` | INI loading with `${ENV}` expansion |

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
