"""Images with Pillow. Always saved as a NEW file; the original is never changed."""
from __future__ import annotations

from pathlib import Path

from jarvis.operator.core import LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap, human_size
from jarvis.operator.files_ops import _unique, walk_files

EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise Unavailable("Image tools need Pillow, which isn't installed in this copy of Jarvis.") from None


def _open(op, path):
    p = op.check_path(path)
    if p.suffix.lower() not in EXT:
        raise OperatorError("That isn't a picture type I handle (jpg, png, gif, bmp, webp, tiff).")
    return p, _pil().open(p)


@cap("image.info", "files_read", "Show an image's size and format", LOW, ("path",), False, "51")
def info(op, path: str = "") -> Outcome:
    p, im = _open(op, path)
    return Outcome("info", f"{p.name}: {im.width} x {im.height} pixels, {im.format}, {human_size(p.stat().st_size)}.", {"w": im.width, "h": im.height})


@cap("image.resize", "documents", "Resize an image into a new file", MEDIUM, ("path", "width", "height", "percent"), False, "51")
def resize(op, path: str = "", width: int = 0, height: int = 0, percent: float = 0) -> Outcome:
    p, im = _open(op, path)
    if percent:
        w, h = int(im.width * percent / 100), int(im.height * percent / 100)
    elif width and height:
        w, h = int(width), int(height)
    elif width:
        w, h = int(width), int(im.height * width / im.width)
    elif height:
        w, h = int(im.width * height / im.height), int(height)
    else:
        raise OperatorError("Tell me a width, a height or a percentage.")
    if not (1 <= w <= 16000 and 1 <= h <= 16000):
        raise OperatorError("That size is out of range.")
    out = _unique(p.with_name(f"{p.stem} - {w}x{h}{p.suffix}"))
    im.resize((w, h), _pil().LANCZOS).save(out)
    ok = _pil().open(out).size == (w, h)
    return Outcome("verified" if ok else "failed", f"Saved {out.name} ({w} x {h}); the original is unchanged.", str(out))


@cap("image.crop", "documents", "Crop an image into a new file", MEDIUM, ("path", "left", "top", "right", "bottom"), False, "51")
def crop(op, path: str = "", left: int = 0, top: int = 0, right: int = 0, bottom: int = 0) -> Outcome:
    p, im = _open(op, path)
    box = (int(left), int(top), int(right or im.width), int(bottom or im.height))
    if not (0 <= box[0] < box[2] <= im.width and 0 <= box[1] < box[3] <= im.height):
        raise OperatorError(f"The crop must fit inside the {im.width} x {im.height} image.")
    out = _unique(p.with_name(f"{p.stem} - cropped{p.suffix}"))
    im.crop(box).save(out)
    return Outcome("verified" if out.exists() else "failed", f"Saved {out.name}.", str(out))


@cap("image.convert", "documents", "Convert an image to another format", MEDIUM, ("path", "to"), False, "51")
def convert(op, path: str = "", to: str = "png") -> Outcome:
    p, im = _open(op, path)
    t = to.lower().lstrip(".")
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP", "gif": "GIF", "tiff": "TIFF"}.get(t)
    if not fmt:
        raise OperatorError("I convert to jpg, png, webp, bmp, gif or tiff.")
    out = _unique(p.with_suffix("." + ("jpg" if t == "jpeg" else t)))
    if out == p:
        out = _unique(p.with_name(p.stem + " - converted." + t))
    img = im.convert("RGB") if fmt == "JPEG" else im
    img.save(out, fmt)
    ok = _pil().open(out).format == fmt
    return Outcome("verified" if ok else "failed", f"Saved {out.name}.", str(out))


@cap("image.compress", "documents", "Make a smaller JPEG copy of an image", MEDIUM, ("path", "quality"), False, "51")
def compress(op, path: str = "", quality: int = 75) -> Outcome:
    p, im = _open(op, path)
    out = _unique(p.with_name(f"{p.stem} - small.jpg"))
    im.convert("RGB").save(out, "JPEG", quality=max(20, min(int(quality), 95)), optimize=True)
    return Outcome("verified" if out.exists() else "failed", f"{out.name} is {human_size(out.stat().st_size)} (was {human_size(p.stat().st_size)}).", str(out))


@cap("image.thumbnail", "documents", "Make a thumbnail", MEDIUM, ("path", "size"), False, "51")
def thumbnail(op, path: str = "", size: int = 256) -> Outcome:
    p, im = _open(op, path)
    s = max(16, min(int(size), 2048))
    im = im.copy()
    im.thumbnail((s, s))
    out = _unique(p.with_name(f"{p.stem} - thumb{p.suffix}"))
    im.save(out)
    return Outcome("verified" if out.exists() else "failed", f"Saved {out.name} ({im.width} x {im.height}).", str(out))


@cap("image.compare", "files_read", "Compare two images and say how different they are", LOW, ("a", "b"), False, "38,51")
def compare(op, a: str = "", b: str = "") -> Outcome:
    Image = _pil()
    from PIL import ImageChops
    pa, ia = _open(op, a)
    pb, ib = _open(op, b)
    if ia.size != ib.size:
        return Outcome("info", f"They differ: {ia.width}x{ia.height} vs {ib.width}x{ib.height}.")
    diff = ImageChops.difference(ia.convert("RGB"), ib.convert("RGB"))
    box = diff.getbbox()
    if not box:
        return Outcome("info", "The two images are pixel-for-pixel identical.")
    hist = diff.convert("L").histogram()
    changed = sum(hist[8:])
    pct = 100.0 * changed / (ia.width * ia.height)
    return Outcome("info", f"{pct:.2f}% of the pixels differ noticeably; the changes are inside the area {box}.", {"percent": pct, "box": box})


@cap("image.inventory", "files_read", "List the images in a folder with their sizes", LOW, ("folder",), False, "51")
def inventory(op, folder: str | None = None) -> Outcome:
    root = op.check_path(folder) if folder else (op.roots()[0] if op.roots() else None)
    if root is None:
        raise OperatorError("No allowed folders exist yet.")
    rows = []
    Image = _pil()
    for p, st in walk_files(root):
        if p.suffix.lower() in EXT:
            try:
                with Image.open(p) as im:
                    rows.append({"name": p.name, "w": im.width, "h": im.height, "bytes": st.st_size})
            except OSError:
                rows.append({"name": p.name, "w": 0, "h": 0, "bytes": st.st_size})
        if len(rows) >= 500:
            break
    return Outcome("info", f"{len(rows)} image(s), {human_size(sum(r['bytes'] for r in rows))}: " + "; ".join(f"{r['name']} {r['w']}x{r['h']}" for r in rows[:8]) if rows else "No images found.", rows)


@cap("media.convert", "documents", "Convert audio or video with ffmpeg (if it is installed)", MEDIUM, ("path", "to"), False, "52")
def media_convert(op, path: str = "", to: str = "mp3") -> Outcome:
    import shutil
    p = op.check_path(path)
    if shutil.which("ffmpeg") is None:
        raise Unavailable("Converting audio and video needs ffmpeg, which isn't installed. I don't download it for you.")
    t = to.lower().lstrip(".")
    if t not in ("mp3", "wav", "mp4", "mkv", "flac", "ogg", "m4a", "webm"):
        raise OperatorError("I convert to mp3, wav, mp4, mkv, flac, ogg, m4a or webm.")
    out = _unique(p.with_suffix("." + t))
    code, _o, err = op.ops.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(p), str(out)], 900)
    ok = code == 0 and out.exists() and out.stat().st_size > 0
    return Outcome("verified" if ok else "failed", f"Saved {out.name}." if ok else "ffmpeg could not convert it: " + err.strip()[:140], str(out))
