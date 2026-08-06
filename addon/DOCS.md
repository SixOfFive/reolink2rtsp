# reolink2rtsp

Serves RTSP for Reolink cameras that have no RTSP.

Some Reolink models — the Lumus E430 among them — expose no RTSP, ONVIF, RTMP
or HTTP API at all. TCP 9000 and Reolink's proprietary Baichuan protocol is the
only way in. This add-on speaks that protocol, pulls the camera's native H.264
stream, and republishes it as ordinary RTSP for Frigate, go2rtc, VLC or
anything else.

**No transcoding.** Frames arrive already encoded and are only re-packetised
into RTP, so CPU cost is negligible even with several cameras.

## Why run it here

Running the bridge on the same machine as Home Assistant means each camera's
stream crosses the LAN exactly once. go2rtc and Frigate then reach it over
loopback. Running it on another host instead makes every stream cross the
network twice.

## Configuration

```yaml
log_level: info
base_port: 8600
mtu: 1400
rtsp_users: "RTSP_USER:RTSP_PASS"
cameras:
  - name: driveway
    host: 192.0.2.10
    username: admin
    password: YOUR-CAMERA-PASSWORD
    stream: sub
    rtsp_port: 8600
```

### Options

| Option | Meaning |
|---|---|
| `rtsp_users` | Logins clients use to pull streams *from* this add-on, `user:pass` comma separated. Not the camera password. |
| `base_port` | First RTSP port; cameras without `rtsp_port` count up from here. Defaults to 8600 to stay clear of go2rtc/Frigate on 8554. |
| `mtu` | RTP payload size. Lower it if you see loss over Wi-Fi or a VPN. |

### Per camera

| Option | Meaning |
|---|---|
| `name` | URL path, letters/digits/`-`/`_` only |
| `host` | Camera IP |
| `username` / `password` | The **camera** login (Reolink default user is `admin`) |
| `stream` | `main`, `extern` or `sub` — served at the bare path |
| `rtsp_port` | Port for this camera |
| `audio` | Serve the camera's AAC audio as a second track (default true) |
| `enabled` | Set false to keep an offline camera configured but idle |
| `bitrate`, `framerate`, `gop` | **Change the camera's own encoder.** Persistent — affects Home Assistant and the Reolink app too. |
| `always_on` | Stay connected even with no viewers |
| `extra_streams` | Which streams get sub-paths; default all three, `none` for only the configured one |

## URLs

Each camera serves its configured stream at the bare path, and its other
encodes on sub-paths:

```
rtsp://RTSP_USER:RTSP_PASS@<ha-ip>:8600/driveway           the configured stream
rtsp://RTSP_USER:RTSP_PASS@<ha-ip>:8600/driveway/sub       640x360     ~0.3 Mbit/s
rtsp://RTSP_USER:RTSP_PASS@<ha-ip>:8600/driveway/extern    896x512     ~1.2 Mbit/s
rtsp://RTSP_USER:RTSP_PASS@<ha-ip>:8600/driveway/main      2560x1440   ~3.2 Mbit/s
```

The camera encodes all three simultaneously, so switching costs nothing.
Connections are made on demand — the add-on only talks to a camera while
something is watching that stream.

Because the add-on shares the host network, use the Home Assistant machine's
own IP, and from go2rtc or Frigate on the same host you can use `127.0.0.1`.

## Using it from Home Assistant

go2rtc is built into recent Home Assistant. Add to its config:

```yaml
streams:
  driveway: rtsp://RTSP_USER:RTSP_PASS@127.0.0.1:8600/driveway
```

Or in Frigate:

```yaml
cameras:
  driveway:
    ffmpeg:
      inputs:
        - path: rtsp://RTSP_USER:RTSP_PASS@127.0.0.1:8600/driveway
          roles: [detect]
        - path: rtsp://RTSP_USER:RTSP_PASS@127.0.0.1:8600/driveway/main
          roles: [record]
```

Detecting on the substream and recording the main stream is the usual split:
detection does not benefit from 2560×1440, and it keeps CPU down.

## Notes

* The camera password is stored by the Supervisor, not in the config file the
  add-on generates.
* Home Assistant's own Reolink integration also talks to these cameras on port
  9000. Running both is fine — the cameras accept several sessions — but every
  stream you serve is one more.
* Ports below 1024 are not usable here; `base_port` defaults to **8600**, deliberately clear of 8554 - Frigate's built-in go2rtc listens there, and with host networking they would collide.
* Updating means rebuilding the add-on, which pulls the current source from
  GitHub.

## Appendix: driving Home Assistant and Frigate over the API

Useful if you have no SSH, developer console or Samba on Home Assistant OS and
need to configure things remotely. All of this works with a long-lived access
token belonging to an owner account.

**The REST Supervisor proxy does not accept long-lived tokens.** `GET
/api/hassio/...` returns 401 regardless of admin rights. The working route is
the WebSocket API:

```json
{"type": "supervisor/api", "endpoint": "/addons/<slug>/info", "method": "get"}
```

That reaches any Supervisor endpoint, which is how you discover an add-on's
state, its network mapping, and its `ingress_entry`.

**Add-on APIs are reachable through ingress, but need a session cookie**, not a
bearer token. Mint one with Supervisor endpoint `/ingress/session` (POST), then
request `http://<ha>:8123<ingress_entry><path>` with header
`Cookie: ingress_session=<token>`. A bearer token alone gets 401.

Frigate's own API is then available, including `GET /api/config/raw`,
`POST /api/config/save?save_option=saveonly` (which validates before writing),
`POST /api/restart`, `GET /api/stats` and `GET /api/<camera>/latest.jpg`.

Two things worth checking before configuring Frigate:

* **How the Frigate add-on is networked.** It commonly runs bridged rather than
  on the host network, publishing only its WebRTC port. If so, `127.0.0.1` from
  inside Frigate is the container, not the host, and reolink2rtsp must be
  addressed by the Home Assistant machine's IP. `/addons/<slug>/info` reports
  `host_network` and the port map.
* **Free disk**, via Supervisor `/host/info`. Recording three 2560x1440 cameras
  is roughly 3 GB/hour; a Home Assistant OS install with ~10 GB free will fill
  in a few hours and take Home Assistant with it. Enable recording only against
  storage sized for it, and prefer detection on the substream.
