# iPhone MVP notes (historical)

> Preserved from the original README. Commands below refer to the pre-0.2 layout
> (`server.py`, `iphone_device.py`); the hardware findings remain valid for the iOS 17+ HID path.


Local browser viewer using persistent DVT screenshots, FastAPI/WebSockets, and direct
CoreDevice Universal HID + Indigo HID. Includes tap, drag, wheel scrolling, **Home
screen**, **App switcher**, **Volume up**, **Volume down**, and **Siri**.
No CoreDevice media stream is requested.

## Developer Mode setup in the viewer

The browser now shows **Developer Mode** status independently of capture/control.
At connection time the app reads the AMFI Developer Mode status key over USB. If
it is confirmed off, the app attempts to reveal the hidden setting once, before
reporting OS compatibility. This also works as a setup check for the iPhone 8 on
iOS 16, even though the current capture/control backend remains incompatible.

When setup is needed, the panel guides the user through:

1. Connect by USB, unlock the phone, and accept **Trust This Computer**.
2. Open **Settings → Privacy & Security → Developer Mode** near the bottom.
   Close and reopen Settings after the reveal request if the entry was missing.
3. Enable Developer Mode and accept **Restart**.
4. Unlock the phone after restarting, confirm **Turn On**, and enter the passcode
   on the phone if requested. The browser never requests or stores the passcode.
5. Click **Check again**. The app also rechecks automatically every three seconds
   while it is waiting for setup or compatibility; it does not continuously query
   Developer Mode during a healthy capture/control session.

**Reveal setting** checks status and retries a failed reveal. Both setup buttons
work before HID is ready and remain separate from input actions. Requests are
limited to one check every five seconds. Unknown/unreadable status is displayed
as unknown, never assumed to mean off, and does not cause a reveal request.
Automatic reveal is attempted once per device/off episode to avoid repeatedly
changing the setting visibility during retries. Failed reveal attempts can be
retried with the button after unlocking/trusting the phone.

Only AMFI's reveal action is used. The application never enables Developer Mode,
reboots the phone, or confirms the post-restart prompt automatically. A successful
reveal means the setting was exposed, not that Developer Mode is enabled. On iOS
16, the compatibility warning remains visible even after Developer Mode is enabled.

## Device compatibility

The implemented CoreDevice/RSD screenshot-and-HID backend requires **iOS 17+** and
the relevant device services. **iPhone 8 on iOS 16.7.10 is not supported by this
backend.** Enabling Developer Mode or starting a tunnel cannot add CoreDevice support
to iOS 16. The app now checks the USB device's OS before attempting a tunnel and
reports the incompatibility instead of repeatedly trying the userspace tunnel.

An iOS 16 implementation would need a legacy screenshot service (or legacy DVT)
for viewing, and a separate touch-control backend such as a properly signed and
installed WebDriverAgent. Those backends are **not implemented or hardware-verified**
in this project. The earlier successful HID tests were on the previously connected
device; they do not establish compatibility with this iPhone 8.

For iOS 17.0–17.3 on Windows/Linux, an available local kernel tunnel can be used.
Keep this command running in an Administrator PowerShell window on Windows:

```powershell
python -m pymobiledevice3 remote tunneld
```

On Linux, run that daemon with `sudo`. Then, in a separate regular terminal:

```powershell
python server.py --tunnel
```

Windows may also need the platform's additional tunnel drivers. Check the daemon's
own output if it cannot create a tunnel. The default connection now falls back to
the local daemon when the userspace tunnel is unavailable. It selects the same USB
phone by UDID and never launches a privileged process automatically. The daemon
remains bound to loopback by default; do not expose it publicly.

For a manually started kernel tunnel, use its printed RSD endpoint:

```powershell
python server.py --rsd HOST PORT
```

These options connect the app to an existing tunnel; they do not create one and
do not solve iOS 16 compatibility. The local-daemon and explicit-RSD paths have
automated mock checks but have not been hardware-verified in this workspace.

## Windows 11 / Python 3.12 setup

Connect the iPhone by USB, unlock it, trust this computer, enable Developer Mode,
and ensure its developer disk image is mounted. Keep the same Apple USB drivers and
device preparation that made your existing `pymobiledevice3` screenshot command work.
Close other screen-control tools before testing an independent session.

In PowerShell, from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

Open **http://127.0.0.1:8000**. Ctrl+C in the server terminal stops the application.
If your prepared Python environment already has the dependencies, `python server.py`
is enough. `python iphone_remote.py` also starts the new server; the original script
was preserved as `iphone_remote.py.bak`.

Only one browser connection is allowed, including viewing. Close its tab to release
the controller, then click Reconnect in another tab. A second tab is refused instead
of silently taking control. Use the image to click, drag, and scroll. Drags are replayed
after mouse release, with their endpoints and duration; this MVP does not replay
curved paths or support multitouch. Home sends the Indigo Consumer/Menu button;
App switcher sends two complete Indigo Home presses, separated by 120 ms, to
request recently opened applications. The earlier bottom-edge swipe-and-hold
did not produce a response on the user's phone. Select an app by clicking its
card once recent apps appear. The old `swipe_up` action remains an alias.
Volume buttons send one 50 ms press per click. Siri sends a one-second press
using the installed package's Siri usage code; Siri must be enabled on the phone.

Configuration:

```powershell
python server.py --width 480 --quality 65 --active-fps 3 --idle-fps 0.5 --bandwidth-mbps 1.5 --port 8001
python server.py --serial YOUR_DEVICE_UDID
```

Open http://127.0.0.1:8001 if using that port. FPS must satisfy
`0.1 <= idle <= active <= 30`; 3 active / 0.5 idle is the default. Interactions raise
capture frequency for two seconds. With no browser, one screenshot every five
seconds checks connection health, and its contents are immediately discarded.

## Resolution and bandwidth controls

The viewer's **Stream settings** panel applies changes without restarting or reconnecting
the phone. Choose a profile, or edit the individual fields and click **Apply**:

| Profile | Short edge / portrait width | JPEG quality | Active / idle fps | Image budget |
|---|---:|---:|---:|---:|
| Low bandwidth | 360 px | 55 | 2 / 0.5 | 1 Mbps |
| Balanced (default) | 480 px | 65 | 3 / 0.5 | 1.5 Mbps |
| Clearer text | 720 px | 75 | 3 / 0.5 | 1.5 Mbps |
| Original screenshot | Native | Not applied | 3 / 0.5 | 1.5 Mbps |

Resolution preserves aspect ratio, uses the short edge when rotated, and never
upscales. Native passes the phone's original compressed bytes through unchanged.
Quality accepts integers 30..90; the budget accepts 0.1..10 Mbps. Idle FPS must not
exceed active FPS. Changes are validated on the server, apply only to the active
controller, and remain in memory until restart. Startup defaults can be set with
`--width` (0/360/480/720), `--quality`, `--active-fps`, `--idle-fps`, and
`--bandwidth-mbps`. Zero width selects Native.

For a **2 Mbps link**, start with Balanced at 1.5 Mbps. The budget paces **average
image payload** with one frame of burst allowance; it is not a packet-level bandwidth
shaper and excludes status messages, WebSocket/TCP/TLS/VPN headers, and other traffic.
Large frames can briefly exceed the target. A lower budget spaces transmissions
farther apart; captures made while waiting are overwritten, not queued. A budget
change also recalculates pacing for the preceding frame. No unauthenticated remote
access has been added; the existing local-only access rules still apply.

Live metrics report output dimensions, compressed KB/frame, original screenshot KB,
image Mbps over roughly two-second windows, and processing time. An unchanged screen
may show zero image Mbps because duplicate frames are suppressed. JPEG is lossy;
increase resolution/quality or choose Native when small text becomes hard to read.

DVT still acquires a full-resolution screenshot over USB. Changed screenshots are
resized and JPEG-encoded entirely in memory by one background worker. Duplicate
screenshots skip this work; touch handling continues on its own task. Old processing
results are discarded if settings or the viewer session change while the worker runs.
There is no growing processing queue. Reduced transmission costs extra CPU and
temporary decoded-image memory; measure these on the Raspberry Pi.

Read-only Windows hardware measurements on the same screen (JPEG quality 65):

| Output | Compressed size | Processing time |
|---|---:|---:|
| Original 1170 × 2532 PNG | 39.0 KB | Less than 1 ms |
| 480 × 1039 JPEG | 8.4 KB | 62 ms |
| 360 × 779 JPEG | 5.1 KB | 31 ms |

These are individual Windows measurements, not a Pi benchmark or a guarantee for
other screen content. Screenshot acquisition was about 171–172 ms in this run.

## HID source findings

The primary reference was the installed **pymobiledevice3 11.44.0**, not upstream
master. In `remote/core_device/screen_stream.py`:

- `ScreenStreamServer._ensure_hid()` opens `UniversalHIDServiceService`, then
  `IndigoHIDService`, via each service's `connect()` method.
- `_handle_touch()` sends touchscreen reports to service 257. A tap is CONTACT,
  50 ms pause, RELEASE. A drag is successive CONTACT reports followed by RELEASE.
- `_handle_button()` sends Home through Indigo using usage page `0x0C`, usage
  code `0x40`, DOWN, 50 ms pause, UP.
- The browser server also *attempts* audio/video startup in its separate preflight
  path and tolerates most failures. That path is not reproduced here.
- `hid_service.touch_session()` explicitly starts a video media stream before
  yielding HID. This application never uses it, `DisplayService`, or the media receiver.

`iphone_device.direct_hid()` reproduces the direct two-service setup. No extra
authentication call appears in `_ensure_hid()`. Package comments describe a media
authentication gate on some configurations, so opening HID or receiving a transport
acknowledgement alone cannot establish physical input acceptance. The user's observed
response with this direct path is recorded below; no security restrictions are bypassed.

RSD is shared for discovery and the persistent userspace tunnel. Source inspection
shows `RemoteService.connect()` creates an independent RemoteXPC connection for each
HID service, and DVT opens a separate Instruments/DTX transport. Screenshot requests
and HID dispatch run in separate tasks without a common request lock. An actual
concurrent capture/input diagnostic also completed.

Upstream references (may differ from the installed version):
[screen_stream.py](https://github.com/doronz88/pymobiledevice3/blob/master/pymobiledevice3/remote/core_device/screen_stream.py),
[hid_service.py](https://github.com/doronz88/pymobiledevice3/blob/master/pymobiledevice3/remote/core_device/hid_service.py),
[display_service.py](https://github.com/doronz88/pymobiledevice3/blob/master/pymobiledevice3/remote/core_device/display_service.py).

## Verification and diagnostics

Observed in this workspace:

| Check | Result |
|---|---|
| Installed runtime | Python 3.12.10; pymobiledevice3 11.44.0; FastAPI 0.115.8; Uvicorn 0.34.0; websockets 10.4; Pillow 11.1.0 |
| Stage A direct HID | Both services connected; Home reports sent without media startup |
| Stage B concurrent capture/input | Bottom-edge swipe sent during 20 DVT captures; average 784.3 ms; five distinct frames; no screenshots saved |
| Physical observation | User confirmed a visible response after Home then swipe-up; actions were not individually distinguished |
| App switcher correction | User confirmed recent-app cards appeared after the revised Indigo Home double-press; the prior swipe-and-hold failed |
| Live HTTP/WebSocket check | Compressed screenshot received and acknowledged; latest measured screenshot 187 ms; duplicates suppressed |
| Automated checks | Thirty-five Python tests passed, including Developer Mode status, reveal-only behavior, retries, cleanup, and setup-before-compatibility; Node viewer-handler checks passed |
| Live resizing | 480 px, 360 px, and Native switched over WebSocket against the connected iPhone; no phone inputs or image files; worker and services shut down cleanly |
| Real-service shutdown | DVT/HID services started and orderly Uvicorn shutdown completed; retained frame cleared |

**Not yet verified:** physical responses to Volume up/down and Siri,
individually observed icon taps, browser drags/wheel gestures,
browser rendering/coordinate mapping on different devices, actual USB unplug/replug,
a clean device session after all other tools have closed, or Raspberry Pi hardware.
Connection-state cleanup and reconnection triggering have automated simulated checks.
The measured latency varies substantially and does not promise 8 fps. The earlier
120.7 ms benchmark in the task brief remains a separate user measurement.

Stage A defaults to a read-only service probe:

```powershell
python verify_device.py
python verify_device.py --stage A --action home
python verify_device.py --stage A --action tap --x 32768 --y 32768
```

Place the phone on a harmless screen before running a tap. Coordinates are normalized
16-bit values, not physical pixels. Observe the **physical iPhone** for every test.
For Stage B:

```powershell
python verify_device.py --stage B --action app_switcher
```

This keeps DVT capture running while dispatching HID and reports timing without
writing images. A changed screenshot is not proof that the requested input caused it.

For Stage C, open the viewer, click a known app icon, verify it opens on the phone,
drag a safe scrollable list, test wheel/Home/App switcher, and resize the browser. For
Stage D, inspect the metrics, disconnect/reconnect USB, and confirm the old screen
disappears and capture/control recover. Close the browser and check that CPU load
falls. Ctrl+C should close the DVT/HID services and tunnel.

Read-only network smoke check while the server runs:

```powershell
python smoke_test.py --port 8000
```

To check live resolution changes and worker/service cleanup, stop the running viewer
first, then run this temporary local server test (default port 8002):

```powershell
python verify_stream.py
```

It checks 480 px JPEG, 360 px JPEG, and Native output without sending phone inputs
or writing screenshots. It also verifies orderly shutdown.

Automated tests use a separate development-only dependency:

```powershell
python -m pip install "httpx>=0.28,<1"
python -m unittest discover -s tests -v
node tests/test_viewer.cjs
```

## Performance and lifecycle

- Native screenshot bytes go directly to the browser without image decoding/re-encoding.
  Reduced-resolution mode uses Pillow in one background worker, without subprocesses
  or disk writes. The browser decodes the resulting JPEG.
- One latest frame is retained. A content digest suppresses identical images.
  Each browser decode must acknowledge its frame before another is sent; intermediate
  captures are overwritten. A stalled decoder is disconnected after ten seconds.
- Input dispatch is independent of capture. One controller action may be in flight;
  excess actions are rejected. Server input queues are bounded, queued actions older
  than 750 ms are discarded, and actions carry the device generation to prevent replay
  across reconnects. The wheel is throttled in the browser.
- A disconnect clears the retained frame and pending actions. Failed capture causes
  the service group to close and the tunnel to reconnect after three seconds. HID
  failures retry independently so viewing can continue. Active gestures make a best
  effort to release contact on failure or cancellation.
- Capture and transmitted FPS are measured over roughly two-second windows.
  Display FPS counts browser image loads. An unchanged screen can have 1 capture fps
  and 0 transmitted/display fps. Image Mbps excludes network overhead. Processing time
  includes scheduling the single worker. Screenshot latency is the most recent DVT request;
  HID dispatch latency includes gesture duration and transport writes; queue latency
  measures waiting before dispatch. Browser round trip includes message and result
  handling. None measures time until UIKit visibly responds.

## Security

The supplied launcher always binds to **127.0.0.1** and has no LAN bind option.
HTTP and WebSocket peers must be loopback addresses. Host validation and same-origin
WebSocket checks reject rebinding/cross-site control; embedded frames are blocked.
Responses have `Cache-Control: no-store`; external scripts, fonts, and analytics are
not used. Control message size, fields, coordinates, duration, and rate are restricted.
No screenshot, input-event, password, or phone-content files are created. Existing
Apple/pymobiledevice3 pairing records remain managed by those tools.

Local processes can still access a local service. This MVP has no user authentication;
do not forward its port or expose it through a proxy. Future VPN access needs an
authenticated session/authorization layer before accepting the controller, plus TLS,
origin policy updates, and suitable HTTP/WebSocket peer handling. Do not simply change
Uvicorn's bind address or disable the local checks.

## Troubleshooting

- **Port in use:** close the old server or select `--port 8001`. An existing process
  is not updated when source files change; restart it to load the new implementation.
- **HID connected but no response:** run the Stage A diagnostics and physically
  observe the device. Stop competing tools, unlock/retrust the phone, and compare
  against the known-working `pymobiledevice3 developer core-device display serve-web`
  in a separate test. Keep media errors separate from touch observations. The MVP
  never tries media streaming as a workaround.
- **No screenshot / reconnecting:** check USB, trust, Developer Mode, DDI, and the
  existing screenshot setup. Service requests time out and retry; error messages
  expose exception types rather than dumping device payloads.
- **Second tab refused:** close the first tab and click Reconnect. This is intentional.
- **Image stays still:** duplicate suppression is expected. Check capture FPS and
  change something harmless on the phone. Frames are shown in the orientation DVT
  supplies; confirm touch alignment after phone rotation (some OS/device combinations
  may need explicit orientation support beyond this MVP).
- **High latency:** lower active FPS, close other device tools, and compare the
  standalone diagnostic. The active FPS setting limits requests; it cannot make screenshot
  acquisition faster than the device permits.

## Raspberry Pi 3B notes

Use a supported Python environment (Python 3.12 is the development target), install
the same requirements, provide Linux Apple USB/usbmuxd support, and pair/trust the
phone using the platform's normal tooling. This code does not run a separate tunnel
daemon. Start with the Balanced defaults, or Low bandwidth at 360 px and 2 fps;
measure CPU/RAM, processing latency, and image Mbps on hardware. Run the Pi headless
and open the browser on another computer through a separately authenticated access
solution. Pillow is the only added image-processing dependency; there is no FFmpeg,
video decoder, or frontend framework. pymobiledevice3 itself has substantial transitive dependencies. Installation
and userspace tunnel support on the Pi's specific OS/architecture remain untested.
