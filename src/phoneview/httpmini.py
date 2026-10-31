"""Just enough HTTP/1.1 for loopback and usbmux sockets (no extra dependencies)."""
import asyncio
import json


class HttpError(RuntimeError):
    def __init__(self, status, message=""):
        super().__init__(f"HTTP {status} {message}".strip())
        self.status = status


async def read_head(reader, limit=65536):
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > limit:
        raise HttpError(0, "header too large")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise HttpError(0, "bad status line")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return int(parts[1]), headers


async def iter_chunked(reader):
    """Yield the payload pieces of a chunked body."""
    while True:
        size_line = await reader.readuntil(b"\r\n")
        size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            await reader.readuntil(b"\r\n")
            return
        data = await reader.readexactly(size)
        await reader.readexactly(2)
        yield data


async def read_body(reader, headers, limit=8 << 20):
    if headers.get("transfer-encoding", "").lower() == "chunked":
        pieces = []
        total = 0
        async for piece in iter_chunked(reader):
            total += len(piece)
            if total > limit:
                raise HttpError(0, "body too large")
            pieces.append(piece)
        return b"".join(pieces)
    if "content-length" in headers:
        length = int(headers["content-length"])
        if length > limit:
            raise HttpError(0, "body too large")
        return await reader.readexactly(length)
    return await reader.read(limit)


def build_request(method, path, body=None, host="localhost", keep_alive=False):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}",
             "Connection: keep-alive" if keep_alive else "Connection: close"]
    data = b""
    if body is not None:
        data = json.dumps(body).encode()
        lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + data


async def request(open_connection, method, path, body=None, timeout=10):
    """One request on a fresh connection: returns (status, headers, body bytes)."""
    reader, writer = await asyncio.wait_for(open_connection(), timeout)
    try:
        writer.write(build_request(method, path, body))
        await writer.drain()
        status, headers = await asyncio.wait_for(read_head(reader), timeout)
        data = await asyncio.wait_for(read_body(reader, headers), timeout)
        return status, headers, data
    finally:
        writer.close()


async def request_json(open_connection, method, path, body=None, timeout=10):
    status, _, data = await request(open_connection, method, path, body, timeout)
    if status >= 400:
        raise HttpError(status, data[:200].decode("utf-8", "replace"))
    return json.loads(data) if data else None
