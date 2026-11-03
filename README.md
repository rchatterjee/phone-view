# PhoneView (phone-controller)

A tool to view and control phones from a laptop, and potentially remotely, for
security clinics. The phone's owner has to unlock the phone and follow the steps
to establish the connection, every time.

View and control a USB-connected phone from a browser. One small Python process
on the host (Windows, Linux, Raspberry Pi, thin client) relays the phone's screen to
a local web page and sends your clicks, drags and typing back to the phone.

| Phone | Video | Control | What you need |
|---|---|---|---|
| Android 5.0+ | Live H.264, up to 60 fps (scrcpy 5.0.1 server) | Multi-touch, keys, typing, scroll, rotate | USB debugging on; `adb` on the host |
| iPhone, iOS 27+ | Live HEVC (CoreDevice stream, pymobiledevice3 serve-web); falls back to screenshots | Touch, buttons | Developer Mode |
| iPhone, iOS 17–26 | Screenshots, ~1–5 fps (DVT) | Live touch, buttons, typing (CoreDevice HID) | Developer Mode |
| iPhone, iOS 15–16 | Screenshots (screenshotr; Developer Disk Image auto-mounted) | Only if WebDriverAgent is installed ([docs/ios-wda.md](https://github.com/rchatterjee/phone-controller/blob/main/docs/ios-wda.md)); then also faster MJPEG video | Developer Mode (iOS 16) |

The host never decodes or re-encodes Android video; it only copies packets, so a
Raspberry Pi or Wyse 3040 is enough. The browser decodes with WebCodecs.

## Install

Python 3.11+. Install it as a standalone tool, in its own environment:

```bash
pipx install phoneview          # or: uv tool install phoneview
```

`pip install phoneview` into a virtual environment works too. PhoneView pins
pymobiledevice3 to 11.49.x. ISDi uses the same range, so ISDi users install both
together with `pip install "isdi-scanner[phoneview]"` (see ISDi's README).

From a checkout, for development:

```bash
python -m venv .venv
.venv/bin/pip install -e .[dev]       # Windows: .venv\Scripts\pip install -e .[dev]
```

Platform pieces:

- **Android:** Android platform-tools (`adb`). Debian/Ubuntu/Raspberry Pi OS:
  `sudo apt install adb` (also installs the udev rules). Windows: install
  platform-tools and put `adb.exe` on `PATH`, or pass `--adb C:\path\adb.exe`.
  The scrcpy server (~90 KB) is downloaded once from the scrcpy GitHub release
  and checked against a pinned SHA-256; on an offline host pass
  `--scrcpy-server /path/to/scrcpy-server-v5.0.1`.
- **iPhone:** Linux needs `usbmuxd` (`sudo apt install usbmuxd`). Windows needs
  Apple's USB driver (the Apple Devices app or iTunes).

On the phone:

- Android: enable **Developer options → USB debugging**, plug in, accept the
  *Allow USB debugging?* prompt. On Xiaomi/HyperOS also enable **USB debugging
  (Security settings)**, otherwise touches are ignored.
- iPhone: unlock, tap **Trust**, and turn on **Developer Mode** (the page walks you through it).

## Run

```bash
phoneview                  # or: python -m phoneview
```

PhoneView prints a link like `http://127.0.0.1:8000/?token=…` and opens it in your
browser. The token is new on every start; without it the page shows "Access denied".
PhoneView detects whether an Android phone or an iPhone is plugged in and switches
automatically. It can also be started from ISDi's **PhoneView** button (see below).

Using the page:

- Click = tap. Drag = swipe; the finger follows the pointer live.
  Ctrl+drag = two-finger pinch around the screen centre (Android).
- Mouse wheel scrolls (Android natively; iPhone via a short swipe).
- Click the screen, then type. Enter, Backspace, arrows and Escape (= Back on
  Android) work, and Ctrl+V pastes. The text box under the buttons does the same.
- Phone buttons (Back, Home, Recent apps, Power, Volume, Siri/Assistant) and
  Android actions (Notifications, Quick settings, Rotate, Screen off/on) appear
  only when the connected phone supports them.
- **Stream settings** adjusts resolution, fps and bit rate (Android), or
  screenshot size, quality, fps and bandwidth budget (iPhone) without reconnecting.
- **Screenshots** (right-hand column): **Take screenshot** captures the phone's
  screen at full resolution (Android `screencap`; iPhone screenshot service), not
  the scaled-down video. Each one is listed with its number and time; add a note,
  move it up or down (or drag it), save the original PNG, or delete it.
  **Download all as PDF** exports them in that order, one per page, with the time,
  device, note and the original's SHA-256. Apps that block screen capture (some
  banking apps) give a black image.

Useful options (`phoneview --help` lists all):

```
--platform auto|android|ios   restrict detection
--serial ID                   choose a device when several are plugged in
--max-size 1280 --max-fps 30 --bit-rate 4     Android video
--ios-video auto|stream|screenshot            iPhone video route
--width 480 --quality 65 --active-fps 3 --idle-fps 0.5 --bandwidth-mbps 1.5   iPhone screenshots
--tunnel | --rsd HOST PORT    iOS 17+ tunnel options (see docs/ios-mvp-notes.md)
--public-origin URL           allow a local reverse proxy's origin (tailscale serve)
--audit-log FILE              append who viewed which phone, and when (JSON lines)
--screenshot-dir DIR          keep screenshots in DIR so they survive a restart (default: memory)
--no-browser                  print the link only
```

### Viewing from another computer: `tailscale serve`

PhoneView always listens on 127.0.0.1. For remote viewing, let Tailscale publish it
inside your tailnet over HTTPS, on the machine the phone is plugged into:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8000
phoneview --public-origin https://laptop.tailnet.ts.net:8443
```

Then open `https://laptop.tailnet.ts.net:8443/?token=…` with the token from the
startup link. HTTPS also enables the browser's video decoder remotely.

- Use `tailscale serve`, **never `tailscale funnel`**: funnel publishes to the internet.
- Restrict who can reach port 8443 with a tailnet access rule (for example,
  only `group:consultants` to `tag:clinic-laptop`).
- Do not change the bind address; the token is not meant to stand alone on a network.

Without Tailscale, an SSH tunnel works the same way:
`ssh -N -L 8000:127.0.0.1:8000 user@host`, then open the link locally.

### Starting from ISDi

ISDi shows a **PhoneView** button when PhoneView is installed with it
(`pip install "isdi-scanner[phoneview]"`), is on its `PATH`, or `ISDI_PHONEVIEW_CMD`
points to it. Pressing it starts PhoneView on port 6201 if it
is not already running, then opens it in a new tab, already signed in. When the
consultant arrived through `tailscale serve`, the link points at
`https://<same host>:8443` (override with `ISDI_PHONEVIEW_PUBLIC_URL`). PhoneView
stops when ISDi stops, including when ISDi is killed. ISDi passes its private
screenshots directory as `--screenshot-dir`, so screenshots outlast a PhoneView
restart and stay with ISDi's other case data. For remote use, also serve
that port: `tailscale serve --bg --https=8443 http://127.0.0.1:6201`.

### Running as a service (Linux)

```ini
# /etc/systemd/system/phoneview.service
[Unit]
Description=PhoneView
After=network.target

[Service]
User=phoneview
ExecStart=/opt/phoneview/.venv/bin/phoneview --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

The service user needs USB access: membership of `plugdev` (Android udev rules) and
access to the usbmuxd socket for iPhones.

## How it works

```
browser (WebCodecs / JPEG, pointer + keys)
   │  one WebSocket: binary video/JPEG frames, JSON status + input
host: session layer (token sign-in, host/origin checks, one controller, backpressure, input validation)
   │  Backend interface: capabilities, video, input, setup actions
   ├─ Android: adb server protocol → scrcpy server on the phone → H.264 passthrough
   ├─ iOS 27+: pymobiledevice3 ScreenStreamServer (in-process, loopback) → HEVC passthrough
   ├─ iOS 17+: DVT screenshots → JPEG + CoreDevice HID
   └─ iOS 15–16: screenshotr / WDA MJPEG → JPEG + WebDriverAgent
```

- `src/phoneview/server.py`: app, security middleware, CLI
- `src/phoneview/security.py`: token sign-in, session cookies, host/origin allow-lists, audit log
- `src/phoneview/session.py`: WebSocket protocol (documented at the top of the file)
- `src/phoneview/media.py`: fan-out; video queues resync on key frames, JPEG is latest-wins with acknowledgements
- `src/phoneview/events.py`: the one input model every backend accepts
- `src/phoneview/backends/`: interface and USB auto-detection
- `src/phoneview/android/`: adb client, scrcpy protocol, backend
- `src/phoneview/ios/`: connection/tunnel, Developer Mode, HID, stream, WDA, backend
- `src/phoneview/static/`: `index.html`, `viewer.js`, `viewer.css` (no build step)

Robustness rules the code follows:

- **Version pinning.** scrcpy server 5.0.1 by SHA-256 (its protocol is internal);
  pymobiledevice3 `>=11.49,<11.50` (CoreDevice APIs move between releases).
- **Capabilities, not special cases.** Each backend reports what works right now,
  and the page shows only those controls.
- **Fallbacks.** Android drops to a smaller size if the encoder refuses one. iPhone
  tries the live stream, then screenshots. A browser that cannot decode HEVC
  switches the iPhone to screenshots by itself.
- **Recovery.** Unplug/replug, viewer reload and settings changes reconnect on their own.
  Stale input never crosses a reconnect (generation numbers). Held fingers are
  released when the viewer leaves.
- **Lightweight.** Android runs the scrcpy server only while someone is watching,
  plus 10 s of grace. iPhone screenshots drop to the idle rate when nothing changes.

## Security

| Layer | What it does |
|---|---|
| Network | Listens on 127.0.0.1 only; nothing is reachable from the LAN. Remote access goes through `tailscale serve` (TLS, tailnet membership, access rules). |
| Sign-in | Per-run token (`PHONEVIEW_TOKEN`, or generated) → random session id in an HttpOnly, SameSite=Strict cookie (Secure over HTTPS). Every page and the WebSocket require it. The token is never a command-line argument, so other users can't read it from the process list. |
| Requests | Host allow-list (blocks DNS rebinding), Origin allow-list on the WebSocket and on every request that changes something (blocks cross-site requests), strict CSP, no caching, one controller at a time. |
| Accountability | Each view's start and end, and each screenshot taken, exported or deleted (with its SHA-256), with the tailnet login that `tailscale serve` adds (`Tailscale-User-Login`, which it strips from incoming requests), go to the log and to `--audit-log`. |
| Data | Only screenshots someone explicitly takes are kept: in memory, or with `--screenshot-dir` in an owner-only (0700) directory. No video frames, keystrokes or clipboard contents are stored. Delete screenshots when the case no longer needs them. |

Not covered: anyone with an account or malware on the host machine can use the phone
through adb or usbmuxd directly, so use a dedicated, up-to-date host.

**After each clinic session, remove the trust the phone keeps:**

- Android: don't tick "Always allow from this computer". Afterwards,
  **Developer options → Revoke USB debugging authorizations**, then turn USB debugging off.
- iPhone: **Settings → General → Transfer or Reset iPhone → Reset → Reset Location &
  Privacy** (forgets trusted computers), then turn Developer Mode off.

## Releasing to PyPI

The version lives in `src/phoneview/__init__.py`. To release, bump it, tag
`vX.Y.Z`, and publish a GitHub release; `.github/workflows/publish.yml` builds, checks
and uploads with PyPI trusted publishing. One-time setup: on PyPI, add a trusted
publisher for this repository, workflow `publish.yml`, environment `pypi`.

To build and check by hand:

```bash
python -m build && python -m twine check dist/*
```

## Tests

```bash
pip install -e .[dev]
python -m unittest discover -s tests
node tests/test_viewer_js.cjs
# Real browser against a fake phone (needs ffmpeg and `pip install playwright`):
python tools/browser_e2e.py
# Against a real phone, while phoneview runs (token from its startup link):
PHONEVIEW_TOKEN=... python tools/smoke_test.py --port 8000
```

What has and has not been verified:

| Check | Status |
|---|---|
| scrcpy 5.0.1 message layouts, checked against the 5.0.1 Java sources | unit tests |
| Android pipeline: adb protocol, jar push, server launch, H.264 relay, touch/key injection, viewer-leave release | end to end against a fake adb + scrcpy server |
| iOS HID dispatch, serve-web stream framing, WDA gesture replay, screenshot loop | unit tests with fakes |
| Viewer: H.264 decode via WebCodecs (~27 fps of a 30 fps clip), JPEG path, click/drag/typing → events | headless Chrome 149, `tools/browser_e2e.py` |
| Google Pixel 10, from a Windows 11 host | **works on hardware** (2026-10-31) |
| Samsung Galaxy Tab S6 (SM-T860, Android 12), from Windows 11 | **works on hardware** (2026-10-31), after shortening the scrcpy launch command (see Troubleshooting) |
| iPhone 8, iOS 16 (screenshotr + DDI auto-mount, view-only without WDA) | **works as expected on hardware** (2026-10-31) |
| WebDriverAgent control, CoreDevice HEVC stream (iOS 27+) | **not yet tested** (no hardware) |
| Raspberry Pi / Wyse 3040 CPU and memory | **not yet measured** |

Known limitations, to confirm on hardware:

- **iOS 17+ touch without a media stream.** pymobiledevice3 notes that some
  configurations ignore HID touch and keyboard reports unless a media stream is
  active. Home and the App switcher were confirmed on hardware earlier. Individual
  taps and drags in screenshot mode were not.
- **Android rotation.** For a fraction of a second after the phone rotates, taps
  can land at transposed positions, and scrcpy ignores a release sent with the old
  frame size.
- **iOS 27+ live stream with no viewer.** The stream keeps the phone awake and
  encoding until PhoneView stops. Android stops 10 s after the last viewer leaves.

Earlier iOS 17 hardware findings (HID behaviour, App switcher, timing) are kept in
[docs/ios-mvp-notes.md](https://github.com/rchatterjee/phone-controller/blob/main/docs/ios-mvp-notes.md).

## Troubleshooting

- **"No Android device found" or "no permissions" (Linux):** install `adb` from apt
  (it adds the udev rules), make sure your user is in `plugdev`, and replug the phone.
- **Samsung: "stack corruption detected (-fstack-protector)" in the log:** Samsung's
  encoder aborts when the scrcpy launch command reaches 255 characters
  ([scrcpy #2841](https://github.com/Genymobile/scrcpy/issues/2841)). PhoneView omits
  options already at their defaults to stay under the limit, and a test enforces it.
  If this appears again, run `adb logcat -d -b crash` and report the output.
- **Android touches do nothing (Xiaomi):** enable *USB debugging (Security settings)*.
- **Black screen on some Android apps:** banking and DRM apps block screen capture. This is expected.
- **"This browser cannot decode …":** use a current Chrome, Edge or Safari, opened at
  `http://127.0.0.1` or `localhost`, not a LAN IP.
- **iPhone 16.x "Could not mount the Developer Disk Image":** keep the phone
  unlocked with Developer Mode on. The first mount downloads the image from GitHub.
- **WSL:** USB devices are not visible inside WSL without `usbipd` passthrough. Run
  PhoneView on Windows directly, or attach the phone with `usbipd attach`.

## License

Apache License 2.0. See [LICENSE](https://github.com/rchatterjee/phone-controller/blob/main/LICENSE).
