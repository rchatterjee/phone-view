"""Screenshots: the store, the PDF export and the HTTP API."""
import io
import json
import os
import re
import stat
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from phoneview.backends.base import ScreenshotUnavailable
from phoneview.screenshots import ScreenshotError, ScreenshotStore
from phoneview.security import Access, AuditLog
from phoneview.server import create_app

from test_server import TOKEN, FakeBackend

DEVICE = {"name": "Google Pixel 10", "os": "Android", "version": "17"}


def png(width=108, height=240, color="#cde"):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


def pdf_pages(data):
    return len(re.findall(rb"/Type /Page\b", data))


class StoreTests(unittest.TestCase):
    def test_add_caption_reorder_delete_in_memory(self):
        store = ScreenshotStore()
        a = store.add(png(), "image/png", DEVICE)
        b = store.add(png(240, 108), "image/png", DEVICE, viewer="Alice <a@x>")
        self.assertEqual((a["width"], a["height"], a["device"], a["device_os"]), (108, 240, "Google Pixel 10", "Android 17"))
        self.assertEqual(len(a["sha256"]), 64)
        self.assertEqual(b["viewer"], "Alice <a@x>")
        store.set_caption(a["id"], "  location sharing on  ")
        self.assertEqual(store.find(a["id"])["caption"], "location sharing on")
        store.reorder([b["id"], a["id"]])
        self.assertEqual([r["id"] for r in store.list()], [b["id"], a["id"]])
        for bad in ([a["id"]], [a["id"], a["id"]], [a["id"], b["id"], "x"], "nope"):
            with self.assertRaises(ScreenshotError):
                store.reorder(bad)
        self.assertEqual(store.image(a["id"]), (png(), "image/png"))
        store.delete(a["id"])
        self.assertEqual([r["id"] for r in store.list()], [b["id"]])
        with self.assertRaises(KeyError):
            store.image(a["id"])
        self.assertTrue(store.thumbnail(b["id"]).startswith(b"\xff\xd8"))

    def test_persists_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "shots")
            store = ScreenshotStore(folder)
            a = store.add(png(), "image/png", DEVICE)
            b = store.add(png(color="#fed"), "image/png", DEVICE)
            store.set_caption(b["id"], "second")
            store.reorder([b["id"], a["id"]])
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
            for name in os.listdir(folder):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode), 0o600, name)
            again = ScreenshotStore(folder)
            self.assertEqual([(r["id"], r["caption"]) for r in again.list()], [(b["id"], "second"), (a["id"], "")])
            again.clear()
            self.assertEqual(sorted(os.listdir(folder)), ["index.json"])

    def test_rejects_non_images_and_unknown_types(self):
        store = ScreenshotStore()
        with self.assertRaises(ScreenshotError):
            store.add(png(), "image/gif")
        with self.assertRaises(Exception):
            store.add(b"not an image", "image/png")
        self.assertEqual(store.list(), [])

    def test_pdf_has_one_page_per_screenshot_in_order(self):
        store = ScreenshotStore()
        self.assertEqual(pdf_pages(store.pdf()), 1)          # an empty export still opens
        a = store.add(png(), "image/png", DEVICE)
        b = store.add(png(240, 108), "image/png", DEVICE)
        store.set_caption(b["id"], "Shared album (Bob) \\ test")
        store.reorder([b["id"], a["id"]])
        data = store.pdf()
        self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
        self.assertEqual(pdf_pages(data), 2)
        self.assertEqual(data.count(b"/Subtype /Image"), 2)
        # Every xref offset points at the object it names.
        xref = int(data.rsplit(b"startxref", 1)[1].split()[0])
        entries = data[xref:].split(b"\n")[3:]
        for number, entry in enumerate(entries, 1):
            if not entry.endswith(b" n "):
                break
            offset = int(entry[:10])
            self.assertTrue(data[offset:].startswith(f"{number} 0 obj".encode()), number)


class ShotBackend(FakeBackend):
    def __init__(self, hub=None):
        super().__init__(hub)
        self.device = dict(DEVICE)
        self.shot = (png(), "image/png")

    async def screenshot(self):
        if isinstance(self.shot, Exception):
            raise self.shot
        return self.shot


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.backend = ShotBackend()
        self.audit_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.audit_file.close()
        self.addCleanup(os.unlink, self.audit_file.name)
        self.store = ScreenshotStore()
        app = create_app(self.backend, Access(TOKEN, 8000), AuditLog(self.audit_file.name), self.store)
        self.client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 5),
                                 follow_redirects=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.assertEqual(self.client.get(f"/?token={TOKEN}").status_code, 303)
        self.origin = {"origin": "http://127.0.0.1:8000"}
        import phoneview.server as server
        self._interval = server.SHOT_INTERVAL
        server.SHOT_INTERVAL = 0
        self.addCleanup(setattr, server, "SHOT_INTERVAL", self._interval)

    def take(self):
        r = self.client.post("/api/screenshots", headers=self.origin)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_take_list_caption_reorder_pdf_delete(self):
        a = self.take()
        self.backend.shot = (png(240, 108), "image/png")
        b = self.take()
        self.assertEqual(a["device"], "Google Pixel 10")
        items = self.client.get("/api/screenshots").json()["items"]
        self.assertEqual([i["id"] for i in items], [a["id"], b["id"]])

        image = self.client.get(f"/api/screenshots/{a['id']}")
        self.assertEqual((image.status_code, image.headers["content-type"], image.content),
                         (200, "image/png", png()))
        self.assertIn("no-store", image.headers["cache-control"])
        dl = self.client.get(f"/api/screenshots/{a['id']}?download=1")
        self.assertIn("attachment", dl.headers["content-disposition"])
        thumb = self.client.get(f"/api/screenshots/{a['id']}?thumb=1")
        self.assertEqual(thumb.headers["content-type"], "image/jpeg")

        r = self.client.patch(f"/api/screenshots/{b['id']}", json={"caption": "Maps sharing"}, headers=self.origin)
        self.assertEqual(r.json()["caption"], "Maps sharing")
        r = self.client.put("/api/screenshots/order", json={"ids": [b["id"], a["id"]]}, headers=self.origin)
        self.assertEqual([i["id"] for i in r.json()["items"]], [b["id"], a["id"]])
        r = self.client.put("/api/screenshots/order", json={"ids": [b["id"]]}, headers=self.origin)
        self.assertEqual(r.status_code, 400)

        pdf = self.client.get("/api/screenshots.pdf")
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertIn("attachment", pdf.headers["content-disposition"])
        self.assertEqual(pdf_pages(pdf.content), 2)

        r = self.client.delete(f"/api/screenshots/{a['id']}", headers=self.origin)
        self.assertEqual([i["id"] for i in r.json()["items"]], [b["id"]])
        self.assertEqual(self.client.get(f"/api/screenshots/{a['id']}").status_code, 404)
        self.assertEqual(self.client.delete("/api/screenshots", headers=self.origin).json(), {"items": []})

        events = [json.loads(line)["event"] for line in open(self.audit_file.name)]
        self.assertEqual(events, ["screenshot", "screenshot", "screenshots_exported",
                                  "screenshot_deleted", "screenshots_deleted"])

    def test_changes_need_same_origin(self):
        a = self.take()
        for method, path, kwargs in (("post", "/api/screenshots", {}),
                                     ("delete", f"/api/screenshots/{a['id']}", {}),
                                     ("patch", f"/api/screenshots/{a['id']}", {"json": {"caption": "x"}}),
                                     ("delete", "/api/screenshots", {})):
            for headers in ({}, {"origin": "https://evil.example"}):
                r = getattr(self.client, method)(path, headers=headers, **kwargs)
                self.assertEqual(r.status_code, 403, (method, path, headers))
        self.assertEqual(len(self.store.items), 1)

    def test_needs_session(self):
        with TestClient(self.client.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 6)) as other:
            self.assertEqual(other.get("/api/screenshots").status_code, 403)
            self.assertEqual(other.post("/api/screenshots", headers=self.origin).status_code, 403)

    def test_errors_are_reported(self):
        self.backend.shot = ScreenshotUnavailable("No Android phone connected")
        r = self.client.post("/api/screenshots", headers=self.origin)
        self.assertEqual((r.status_code, r.json()["error"]), (409, "No Android phone connected"))
        self.assertEqual(self.client.get("/api/screenshots/zzzz").status_code, 404)
        self.assertEqual(self.client.get("/api/screenshots/0123456789abcdef").status_code, 404)

    def test_rate_limited(self):
        import phoneview.server as server
        server.SHOT_INTERVAL = 60
        self.take()
        r = self.client.post("/api/screenshots", headers=self.origin)
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
