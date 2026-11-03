"""Screenshots taken during a viewing session, kept in order, exportable as one PDF.

Each screenshot is the phone's own full-resolution capture (see Backend.screenshot),
stored as the original image plus a small record: when it was taken, from which
device, its SHA-256, and a caption the consultant can edit. The order is the
consultant's: they can move screenshots up and down before exporting.

With a directory (ISDi passes its private screenshots directory) the images and
an index.json survive a PhoneView restart; without one they live in memory until
PhoneView exits. Files are written owner-only (0600 in a 0700 directory).
"""
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import zlib
from pathlib import Path

MAX_SCREENSHOTS = 300
MAX_CAPTION = 500
ID_RE = re.compile(r"^[0-9a-f]{16}$")
MIME_EXT = {"image/png": "png", "image/jpeg": "jpg"}


class ScreenshotError(ValueError):
    pass


def image_size(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.size


class ScreenshotStore:
    def __init__(self, directory=None):
        self.dir = Path(directory) if directory else None
        self.items = []                 # records, in the consultant's order
        self._memory = {}               # id -> bytes when there is no directory
        self._thumbs = {}               # id -> small JPEG for the sidebar
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            self._load()

    # -- persistence -------------------------------------------------------------

    def _index(self):
        return self.dir / "index.json"

    def _load(self):
        try:
            items = json.loads(self._index().read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            items = []
        self.items = [r for r in items
                      if isinstance(r, dict) and ID_RE.match(str(r.get("id", "")))
                      and r.get("mime") in MIME_EXT and self._path(r).exists()]

    def _save_index(self):
        if self.dir is None:
            return
        tmp = self._index().with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            json.dump({"items": self.items}, fh, indent=1)
        os.replace(tmp, self._index())

    def _path(self, record):
        return self.dir / f"{record['id']}.{MIME_EXT[record['mime']]}"

    # -- operations --------------------------------------------------------------

    def list(self):
        return [dict(r) for r in self.items]

    def find(self, sid):
        for record in self.items:
            if record["id"] == sid:
                return record
        raise KeyError(sid)

    def add(self, data, mime, device=None, viewer=None):
        if mime not in MIME_EXT:
            raise ScreenshotError(f"Unsupported image type {mime}")
        if len(self.items) >= MAX_SCREENSHOTS:
            raise ScreenshotError(f"At most {MAX_SCREENSHOTS} screenshots; delete some first.")
        width, height = image_size(data)
        now = datetime.datetime.now().astimezone()
        record = {
            "id": secrets.token_hex(8), "mime": mime, "taken": now.isoformat(timespec="seconds"),
            "device": (device or {}).get("name") or "", "device_os": " ".join(
                str(v) for v in ((device or {}).get("os"), (device or {}).get("version")) if v),
            "width": width, "height": height, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "caption": "", "viewer": viewer or "",
        }
        if self.dir is None:
            self._memory[record["id"]] = data
        else:
            with open(os.open(self._path(record), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
                fh.write(data)
        self.items.append(record)
        self._save_index()
        return dict(record)

    def image(self, sid):
        record = self.find(sid)
        if self.dir is None:
            return self._memory[sid], record["mime"]
        return self._path(record).read_bytes(), record["mime"]

    def thumbnail(self, sid, edge=240):
        """A small JPEG of the screenshot for the sidebar (cached)."""
        if sid not in self._thumbs:
            from PIL import Image
            data, _ = self.image(sid)
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                im.thumbnail((edge, edge * 3))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=80)
            self._thumbs[sid] = buf.getvalue()
        return self._thumbs[sid]

    def set_caption(self, sid, caption):
        if not isinstance(caption, str):
            raise ScreenshotError("Caption must be text")
        record = self.find(sid)
        record["caption"] = caption.strip()[:MAX_CAPTION]
        self._save_index()
        return dict(record)

    def reorder(self, ids):
        current = [r["id"] for r in self.items]
        if not isinstance(ids, list) or sorted(map(str, ids)) != sorted(current):
            raise ScreenshotError("The new order must list every screenshot exactly once")
        by_id = {r["id"]: r for r in self.items}
        self.items = [by_id[i] for i in ids]
        self._save_index()

    def delete(self, sid):
        record = self.find(sid)
        self.items.remove(record)
        self._thumbs.pop(sid, None)
        if self.dir is None:
            self._memory.pop(sid, None)
        else:
            try:
                self._path(record).unlink()
            except FileNotFoundError:
                pass
        self._save_index()

    def clear(self):
        for record in list(self.items):
            self.delete(record["id"])

    def pdf(self, title="PhoneView screenshots"):
        """All screenshots, in order, one per page, with their details."""
        return build_pdf(title, [(dict(r), *self.image(r["id"])) for r in self.items])


# -- a small PDF writer ------------------------------------------------------------
# Pillow could write a PDF, but it keeps every page image in memory and cannot add
# real (selectable) text. This writes one JPEG image and a few lines of Helvetica
# text per page, one page at a time.

PAGE_W, PAGE_H, MARGIN = 595.0, 842.0, 36.0    # A4 in points
FONT_SIZE, LEADING = 9.5, 12.5


def _pdf_text(text):
    """A PDF literal string in WinAnsi (Helvetica): other characters become '?'."""
    raw = text.encode("cp1252", "replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _wrap(text, width_chars):
    out = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split(" "):
            if line and len(line) + 1 + len(word) > width_chars:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        out.append(line)
    return out


def _as_jpeg(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        rgb = im.convert("RGB")
        buf = io.BytesIO()
        # 4:4:4 at high quality keeps on-screen text crisp.
        rgb.save(buf, "JPEG", quality=92, subsampling=0, optimize=True)
        return buf.getvalue(), rgb.size


def _when(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S %z")
    except (TypeError, ValueError):
        return str(iso)


def build_pdf(title, shots):
    """shots: [(record, image bytes, mime)] -> PDF bytes."""
    out = io.BytesIO()
    offsets = []

    def obj(body):
        offsets.append(out.tell())
        out.write(f"{len(offsets)} 0 obj\n".encode() + body + b"\nendobj\n")
        return len(offsets)

    def stream(header, data):
        return obj(header + f" /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")

    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    catalog_id, pages_id, font_id = 1, 2, 3
    offsets.extend([0, 0])                       # catalog and pages are written last
    obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids = []
    total = len(shots)
    exported = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    if not shots:
        shots_iter = [(None, None, None)]
    else:
        shots_iter = shots
    for number, (record, data, _mime) in enumerate(shots_iter, 1):
        lines = []
        image_id = None
        if record is None:
            lines = [title, "", "No screenshots were taken."]
        else:
            jpeg, (w, h) = _as_jpeg(data)
            image_id = stream(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                              "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode".encode(), jpeg)
            device = " - ".join(x for x in (record.get("device"), record.get("device_os")) if x)
            lines.append(f"Screenshot {number} of {total}    {_when(record.get('taken'))}"
                         + (f"    {device}" if device else ""))
            if record.get("caption"):
                lines += _wrap("Note: " + record["caption"], 105)
            lines.append(f"Original: {record['width']}x{record['height']} {record['mime']}, "
                         f"SHA-256 {record['sha256']}")
        footer = f"{title} - exported {exported} - page {number} of {max(total, 1)}"

        ops = [b"BT", f"/F1 {FONT_SIZE} Tf".encode()]
        y = PAGE_H - MARGIN - FONT_SIZE
        for line in lines:
            ops.append(f"1 0 0 1 {MARGIN} {y:.1f} Tm ".encode() + _pdf_text(line) + b" Tj")
            y -= LEADING
        ops.append(f"1 0 0 1 {MARGIN} {MARGIN - 18:.1f} Tm ".encode() + _pdf_text(footer) + b" Tj")
        ops.append(b"ET")
        if image_id is not None:
            top = y - 6
            box_w, box_h = PAGE_W - 2 * MARGIN, top - MARGIN
            scale = min(box_w / w, box_h / h)
            dw, dh = w * scale, h * scale
            x0 = MARGIN + (box_w - dw) / 2
            y0 = top - dh
            ops.append(f"q {dw:.2f} 0 0 {dh:.2f} {x0:.2f} {y0:.2f} cm /Im{number} Do Q".encode())
            ops.append(f"q 0.6 G 0.5 w {x0:.2f} {y0:.2f} {dw:.2f} {dh:.2f} re S Q".encode())
        content = zlib.compress(b"\n".join(ops))
        content_id = stream(b"<< /Filter /FlateDecode", content)
        xobject = f"/XObject << /Im{number} {image_id} 0 R >>" if image_id else ""
        page_ids.append(obj(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> {xobject} >> /Contents {content_id} 0 R >>".encode()))

    # Catalog and page tree.
    offsets[catalog_id - 1] = out.tell()
    out.write(f"{catalog_id} 0 obj\n<< /Type /Catalog /Pages {pages_id} 0 R >>\nendobj\n".encode())
    offsets[pages_id - 1] = out.tell()
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    out.write(f"{pages_id} 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n".encode())
    info_id = len(offsets) + 1
    offsets.append(out.tell())
    out.write(f"{info_id} 0 obj\n<< /Title ".encode() + _pdf_text(title) + b" /Producer (PhoneView) >>\nendobj\n")

    xref = out.tell()
    out.write(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(offsets) + 1} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
