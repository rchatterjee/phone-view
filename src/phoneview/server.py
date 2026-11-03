"""PhoneView: local browser viewer and controller for USB-connected phones.

Run: phoneview   (it prints, and opens, http://127.0.0.1:8000/?token=...)
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from starlette.middleware.trustedhost import TrustedHostMiddleware

from phoneview import __version__, session
from phoneview.backends.base import ScreenshotUnavailable
from phoneview.screenshots import ID_RE as SHOT_ID, ScreenshotError, ScreenshotStore
from phoneview.security import COOKIE, FORBIDDEN_PAGE, Access, AuditLog, is_loopback, viewer_identity

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"viewer.js": "text/javascript", "viewer.css": "text/css"}


SHOT_INTERVAL = 0.7      # seconds between screenshots (a double click takes one)


def create_app(backend, access, audit=None, screenshots=None):
    audit = audit or AuditLog()
    shots = screenshots if screenshots is not None else ScreenshotStore()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(backend.run())
        try:
            yield
        finally:
            task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                await backend.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.controller = None
    app.state.backend = backend
    app.state.access = access

    @app.middleware("http")
    async def guard(request, call_next):
        # Only this machine connects: browsers on it, or the tailscale serve proxy.
        if not request.client or not is_loopback(request.client.host):
            return PlainTextResponse("Local access only", status_code=403)
        if request.url.path.startswith("/static/"):
            response = await call_next(request)          # no device or client data
        elif access.token_matches(request.query_params.get("token")):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, access.new_session(), httponly=True, samesite="strict",
                                secure=access.cookie_secure(request.headers.get("host")), path="/")
        elif not access.valid_session(request.cookies.get(COOKIE)):
            response = HTMLResponse(FORBIDDEN_PAGE, status_code=403)
        elif request.method not in ("GET", "HEAD") and not access.origin_allowed(request.headers.get("origin", "")):
            # Changes only from PhoneView's own page (the session cookie is SameSite=Strict too).
            response = JSONResponse({"error": "Same-origin request required"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; script-src 'self'; "
                                       "style-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        })
        return response

    # Added last so it runs first: only our own host names reach the app (blocks DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=access.allowed_hosts)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in STATIC_FILES:
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(STATIC / name, media_type=STATIC_FILES[name])

    # -- screenshots ---------------------------------------------------------------

    def viewer_of(request):
        return viewer_identity(request.headers, request.client.host if request.client else "")

    def shot_or_404(sid):
        if not SHOT_ID.match(sid):
            raise HTTPException(404)
        try:
            return shots.find(sid)
        except KeyError:
            raise HTTPException(404) from None

    last_shot = [0.0]
    shot_lock = asyncio.Lock()

    @app.get("/api/screenshots")
    async def list_screenshots():
        return {"items": shots.list()}

    @app.post("/api/screenshots")
    async def take_screenshot(request: Request):
        async with shot_lock:
            now = asyncio.get_running_loop().time()
            if now - last_shot[0] < SHOT_INTERVAL:
                return JSONResponse({"error": "Please wait a moment between screenshots."}, status_code=429)
            last_shot[0] = now
            try:
                data, mime = await backend.screenshot()
                device = (backend.full_status().get("device") or {})
                record = await asyncio.to_thread(shots.add, data, mime, device, viewer_of(request))
            except (ScreenshotUnavailable, ScreenshotError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            except Exception as exc:
                logging.getLogger(__name__).warning("Screenshot failed: %s", type(exc).__name__)
                return JSONResponse({"error": f"Screenshot failed ({type(exc).__name__})"}, status_code=500)
        audit.write("screenshot", viewer=record["viewer"], device=record["device"],
                    id=record["id"], sha256=record["sha256"])
        return record

    @app.get("/api/screenshots.pdf")
    async def screenshots_pdf(request: Request):
        pdf = await asyncio.to_thread(shots.pdf)
        audit.write("screenshots_exported", viewer=viewer_of(request), count=len(shots.items))
        name = time.strftime("phoneview-screenshots-%Y%m%d-%H%M.pdf")
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.put("/api/screenshots/order")
    async def reorder_screenshots(request: Request):
        body = await small_json(request)
        try:
            shots.reorder(body.get("ids"))
        except ScreenshotError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"items": shots.list()}

    @app.delete("/api/screenshots")
    async def delete_all_screenshots(request: Request):
        count = len(shots.items)
        shots.clear()
        audit.write("screenshots_deleted", viewer=viewer_of(request), count=count)
        return {"items": []}

    @app.get("/api/screenshots/{sid}")
    async def screenshot_image(sid: str, download: int = 0, thumb: int = 0):
        record = shot_or_404(sid)
        if thumb:
            return Response(await asyncio.to_thread(shots.thumbnail, sid), media_type="image/jpeg")
        data, mime = shots.image(sid)
        headers = {}
        if download:
            stamp = record["taken"][:19].replace(":", "").replace("T", "-")
            ext = "png" if mime == "image/png" else "jpg"
            headers["Content-Disposition"] = f'attachment; filename="screenshot-{stamp}-{sid[:6]}.{ext}"'
        return Response(data, media_type=mime, headers=headers)

    @app.patch("/api/screenshots/{sid}")
    async def caption_screenshot(sid: str, request: Request):
        shot_or_404(sid)
        body = await small_json(request)
        try:
            return shots.set_caption(sid, body.get("caption"))
        except ScreenshotError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.delete("/api/screenshots/{sid}")
    async def delete_screenshot(sid: str, request: Request):
        record = shot_or_404(sid)
        shots.delete(sid)
        audit.write("screenshot_deleted", viewer=viewer_of(request), id=sid, sha256=record["sha256"])
        return {"items": shots.list()}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        peer = ws.client.host if ws.client else ""
        if not is_loopback(peer) or not access.origin_allowed(ws.headers.get("origin", "")):
            await ws.close(code=1008, reason="Same-origin browser required")
            return
        if not access.valid_session(ws.cookies.get(COOKIE)):
            await ws.close(code=1008, reason="Not signed in: open the PhoneView link again")
            return
        if app.state.controller is not None:
            await ws.close(code=1008, reason="Another browser controls this phone")
            return
        # Claim before the first await: two connections cannot both become controller.
        app.state.controller = ws
        viewer = viewer_identity(ws.headers, peer)
        device = (backend.full_status().get("device") or {}).get("name")
        audit.write("view_start", viewer=viewer, device=device)
        started = asyncio.get_running_loop().time()
        try:
            await session.serve(backend, ws, viewer=viewer)
        finally:
            app.state.controller = None
            audit.write("view_end", viewer=viewer, device=device,
                        seconds=round(asyncio.get_running_loop().time() - started))

    return app


async def small_json(request, limit=64 * 1024):
    body = await request.body()
    if len(body) > limit:
        raise HTTPException(413)
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400) from None
    if not isinstance(value, dict):
        raise HTTPException(400)
    return value


def build_backend(args):
    from phoneview.android.adb import AdbClient
    from phoneview.android.backend import AndroidBackend, AndroidSettings
    from phoneview.backends.auto import AutoBackend, detect_android, detect_ios
    from phoneview.ios.backend import IOSBackend, IOSSettings

    android_settings = AndroidSettings(max_size=args.max_size, max_fps=args.max_fps,
                                       bit_rate_mbps=args.bit_rate, serial=args.serial,
                                       server_path=args.scrcpy_server)
    rsd_address = None
    if args.rsd:
        rsd_address = (args.rsd[0], int(args.rsd[1]))
        if not 1 <= rsd_address[1] <= 65535:
            raise ValueError("RSD port must be in 1..65535")
    ios_settings = IOSSettings(active_fps=args.active_fps, idle_fps=args.idle_fps, serial=args.serial,
                               short_edge=args.width, jpeg_quality=args.quality,
                               bandwidth_mbps=args.bandwidth_mbps, tunnel=args.tunnel,
                               rsd_address=rsd_address, video_mode=args.ios_video,
                               auto_mount_ddi=not args.no_auto_mount)
    adb = AdbClient(adb_path=args.adb)
    factories = {
        "android": lambda hub: AndroidBackend(android_settings, hub, adb),
        "ios": lambda hub: IOSBackend(ios_settings, hub),
    }
    if args.platform == "android":
        return factories["android"](None)
    if args.platform == "ios":
        return factories["ios"](None)
    detectors = {"android": lambda: detect_android(adb), "ios": detect_ios}
    return AutoBackend(factories, detectors=detectors)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="phoneview", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", choices=["auto", "android", "ios"], default="auto",
                        help="Which phone type to look for (default: whichever is plugged in)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--public-origin", action="append", default=[], metavar="URL",
                        help="Origin of a reverse proxy on this machine, e.g. the tailscale serve URL "
                             "https://laptop.tailnet.ts.net:8443 (repeatable)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser at startup")
    parser.add_argument("--audit-log", metavar="FILE", help="Append view start/end records (JSON lines)")
    parser.add_argument("--screenshot-dir", metavar="DIR",
                        help="Keep screenshots in DIR (owner-only) so they survive a restart; "
                             "default: in memory until PhoneView exits")
    parser.add_argument("--exit-on-stdin-eof", action="store_true",
                        help="Exit when stdin closes (used when another program, such as ISDi, launches PhoneView)")
    parser.add_argument("--serial", help="adb serial or iPhone UDID to use when several are connected")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"phoneview {__version__}")

    android = parser.add_argument_group("Android (scrcpy)")
    android.add_argument("--max-size", type=int, default=1280, help="Video long edge in px; 0 = native")
    android.add_argument("--max-fps", type=int, default=30)
    android.add_argument("--bit-rate", type=float, default=4.0, help="Video bit rate in Mbps (0.5..16)")
    android.add_argument("--adb", help="Path to the adb binary (default: from PATH or $ADB)")
    android.add_argument("--scrcpy-server", help="Path to scrcpy-server v5.0.1 (default: download once, SHA-256 checked)")

    ios = parser.add_argument_group("iPhone")
    ios.add_argument("--ios-video", choices=["auto", "stream", "screenshot"], default="auto",
                     help="auto: try the CoreDevice video stream (iOS 27+), fall back to screenshots")
    ios.add_argument("--width", type=int, default=480, choices=[0, 360, 480, 720],
                     help="Screenshot short edge in pixels; 0 passes original bytes")
    ios.add_argument("--quality", type=int, default=65, help="JPEG quality: 30..90")
    ios.add_argument("--active-fps", type=float, default=3)
    ios.add_argument("--idle-fps", type=float, default=0.5)
    ios.add_argument("--bandwidth-mbps", type=float, default=1.5, help="Average image budget: 0.1..10 Mbps")
    ios.add_argument("--no-auto-mount", action="store_true",
                     help="iOS 15-16: do not download/mount the Developer Disk Image automatically")
    connection = ios.add_mutually_exclusive_group()
    connection.add_argument("--tunnel", action="store_true", help="iOS 17+: use the local privileged tunneld")
    connection.add_argument("--rsd", nargs=2, metavar=("HOST", "PORT"), help="iOS 17+: RSD endpoint of a manual tunnel")
    return parser, parser.parse_args(argv)


def server_config(app, port):
    # Always loopback: remote viewers come through a local proxy such as tailscale serve.
    # proxy_headers=False: uvicorn would otherwise replace the client address with the
    # proxy's X-Forwarded-For (the viewer's tailnet IP), and the loopback check would
    # refuse every viewer coming through tailscale serve ("Local access only").
    return uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False,
                          ws_max_size=4096, ws_max_queue=32, ws_per_message_deflate=False,
                          access_log=False, timeout_graceful_shutdown=5, log_level="warning")


def watch_stdin(server):
    """Stop when the launching program goes away, however it ended (its end of the pipe closes)."""
    def wait():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        logging.getLogger(__name__).info("Parent closed stdin; shutting down")
        server.should_exit = True
        fallback = threading.Timer(10, os._exit, args=(0,))   # if a clean shutdown hangs
        fallback.daemon = True
        fallback.start()
    threading.Thread(target=wait, name="stdin-watch", daemon=True).start()


def main(argv=None):
    parser, args = parse_args(argv)
    token = os.environ.get("PHONEVIEW_TOKEN") or Access.generate_token()
    try:
        backend = build_backend(args)
        access = Access(token, args.port, args.public_origin)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    link = f"http://127.0.0.1:{args.port}/?token={token}"
    if "PHONEVIEW_TOKEN" not in os.environ:
        print(f"PhoneView: open {link}")
    for origin in args.public_origin:
        print(f"PhoneView: also reachable through the proxy at {origin} (append ?token=...)")
    app = create_app(backend, access, AuditLog(args.audit_log), ScreenshotStore(args.screenshot_dir))
    server = uvicorn.Server(server_config(app, args.port))
    if args.exit_on_stdin_eof:
        watch_stdin(server)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(link,)).start()
    server.run()


if __name__ == "__main__":
    main()
