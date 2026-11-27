"""Image input: turn a file on disk into an OpenAI image part a vision model can read.

Stdlib only. An image under the caps is sent untouched; a bigger one is scaled to a
1024 px JPEG by `ffmpeg`. No ffmpeg and over the cap = a sentence the model can act on.

Caps: long edge <= MAX_EDGE and base64 payload <= MAX_B64. The image stays in the
history and is re-sent every turn, so it is a context cost, not a side channel.
1024 px is enough to read a UI and judge a layout.
"""

from __future__ import annotations

import base64
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

MAX_EDGE = 1024          # px, long edge after downscaling
MAX_B64 = 1_200_000      # base64 chars allowed for one image (~900 KB binary)
MIME = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif",
        "webp": "image/webp", "bmp": "image/bmp"}


class Unsupported(Exception):
    """Not an image this module can prepare, with a reason worth showing the model."""


def kind(data: bytes) -> str:
    """Image type from the magic bytes, never from the file name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    raise Unsupported("not an image (no PNG/JPEG/GIF/WEBP/BMP magic bytes)")


def size(data: bytes, k: str) -> tuple[int, int] | None:
    """Width and height from the header alone; None when the header does not say."""
    try:
        if k == "png":
            return struct.unpack(">II", data[16:24])
        if k == "gif":
            return struct.unpack("<HH", data[6:10])
        if k == "bmp":
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
        if k == "jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except struct.error:
        pass
    return None  # webp, or a header we cannot read: ffmpeg decides


def _ffmpeg_jpeg(path: Path, max_edge: int) -> bytes | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.jpg"
        cmd = [exe, "-v", "error", "-y", "-i", str(path), "-frames:v", "1", "-q:v", "4",
               "-vf", f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease",
               str(out)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError):
            return None
        return out.read_bytes() if r.returncode == 0 and out.exists() else None


def prepare(path: str | Path, max_edge: int = MAX_EDGE) -> tuple[dict, dict]:
    """(image_part, info): the OpenAI image part, and what was sent for describe()."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise Unsupported(f"cannot read {p.name}: {e.strerror or e}") from None
    if not data:
        raise Unsupported(f"{p.name} is empty")
    k = kind(data)
    wh = size(data, k)
    info = {"file": p.name, "kind": k, "size": wh, "bytes": len(data), "sent": None}
    if wh and max(wh) <= max_edge and len(data) * 4 / 3 <= MAX_B64:
        sent, k_sent = data, k
    else:
        sent, k_sent = _ffmpeg_jpeg(p, max_edge), "jpeg"
        if sent is None or len(sent) * 4 / 3 > MAX_B64:
            dims = f"{wh[0]}x{wh[1]}, " if wh else ""
            raise Unsupported(
                f"{k.upper()} is {dims}{len(data) // 1024} KB, over the {max_edge} px / "
                f"{MAX_B64 // 1000} KB cap, and ffmpeg could not shrink it (missing or failed). "
                f"Downscale it to a PNG or JPEG under {max_edge} px first.")
        info["sent"] = (size(sent, "jpeg"), len(sent))
    part = {"type": "image_url",
            "image_url": {"url": f"data:{MIME[k_sent]};base64,{base64.b64encode(sent).decode()}"}}
    return part, info


def describe(info: dict) -> str:
    """One line telling the model what it is looking at."""
    dims = f"{info['size'][0]}x{info['size'][1]} " if info["size"] else ""
    s = f"{dims}{info['kind'].upper()}, {info['bytes'] / 1024:.0f} KB"
    if info["sent"]:
        wh, n = info["sent"]
        s += f", sent as {f'{wh[0]}x{wh[1]} ' if wh else ''}JPEG, {n / 1024:.0f} KB"
    return s
