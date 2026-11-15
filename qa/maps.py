"""Scanned-map tools (non-generative): tiled OCR with word boxes, label lookup, fill-colour sampling.

Generic: works on any large scanned sheet. No model besides tesseract. Results are cacheable JSON.
  words = ocr_words(path)                 # [{text, conf, x, y, w, h}] in full-resolution pixels
  hits  = find_label(words, "Fire Eng")   # fuzzy phrase search on reconstructed text lines
  fill_at(path, box)                      # dominant non-ink colour around a box -> (rgb, name)
  crop(path, box, out, pad)               # save a crop for eyeballing
"""
from __future__ import annotations

import colorsys
import difflib
import json
from pathlib import Path


def _tiles(w: int, h: int, tile: int, overlap: int):
    step = tile - overlap
    for y in range(0, max(h - overlap, 1), step):
        for x in range(0, max(w - overlap, 1), step):
            yield x, y, min(x + tile, w), min(y + tile, h)


def _ink(im):
    """Grey image where only dark ink is dark: max(R,G,B) keeps yellow/pink/blue fills bright (plain luminance darkens them)."""
    import numpy as np
    from PIL import Image

    return Image.fromarray(np.asarray(im).max(axis=2))


def _back(r, rot, cw, ch):
    """Map a word box from a rotated crop (rot = CCW degrees) back to the unrotated crop frame."""
    x, y, w, h = r
    if rot == 90:
        return cw - y - h, x, h, w
    if rot == 270:
        return y, ch - x - w, h, w
    return x, y, w, h


def ocr_words(path: Path, tile: int = 1500, overlap: int = 250, psm: int = 11, min_conf: int = 35, lang: str | None = None,
              scale: float = 2.0, rotations=(0, 90, 270)) -> list[dict]:
    """Tiled OCR at native resolution, ink-channel + upscale, several rotations; sparse-text mode finds scattered map labels."""
    import pytesseract
    from PIL import Image, ImageOps

    from qa.ocr import _langs

    Image.MAX_IMAGE_PIXELS = None
    lang = lang or _langs()
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    W, H = im.size
    ink = _ink(im)
    words: list[dict] = []
    for x0, y0, x1, y1 in _tiles(W, H, tile, overlap):
        base = ImageOps.autocontrast(ink.crop((x0, y0, x1, y1)), cutoff=1)
        cw, ch = base.size
        base = base.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
        for rot in rotations:
            img = base.rotate(rot, expand=True) if rot else base
            d = pytesseract.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
            for i, t in enumerate(d["text"]):
                t = t.strip()
                if len(t) < 2 and not t.isdigit() or not t or float(d["conf"][i]) < min_conf:
                    continue
                box = tuple(v / scale for v in (d["left"][i], d["top"][i], d["width"][i], d["height"][i]))
                bx, by, bw, bh = _back(box, rot, cw, ch)
                words.append(dict(text=t, conf=float(d["conf"][i]), x=int(x0 + bx), y=int(y0 + by), w=int(bw), h=int(bh), rot=rot))
    words.sort(key=lambda r: (-r["conf"]))
    kept: list[dict] = []
    for r in words:
        cx, cy = r["x"] + r["w"] / 2, r["y"] + r["h"] / 2
        if any(k["text"] == r["text"] and abs(k["x"] + k["w"] / 2 - cx) < max(r["h"], r["w"], 8) and abs(k["y"] + k["h"] / 2 - cy) < max(r["h"], r["w"], 8) for k in kept):
            continue
        kept.append(r)
    kept.sort(key=lambda r: (r["y"], r["x"]))
    return kept


def lines(words: list[dict], ytol: float = 0.6, gap: float = 2.5) -> list[dict]:
    """Group words into text lines (same baseline band, small horizontal gaps) with a joint bounding box."""
    out: list[list[dict]] = []
    for r in sorted(words, key=lambda r: (r["y"] + r["h"] / 2, r["x"])):
        for ln in out:
            ref = ln[-1]
            if abs((r["y"] + r["h"] / 2) - (ref["y"] + ref["h"] / 2)) < ytol * max(r["h"], ref["h"]) and 0 <= r["x"] - (ref["x"] + ref["w"]) < gap * max(r["h"], ref["h"]):
                ln.append(r)
                break
        else:
            out.append([r])
    res = []
    for ln in out:
        x0, y0 = min(r["x"] for r in ln), min(r["y"] for r in ln)
        x1, y1 = max(r["x"] + r["w"] for r in ln), max(r["y"] + r["h"] for r in ln)
        res.append(dict(text=" ".join(r["text"] for r in ln), x=x0, y=y0, w=x1 - x0, h=y1 - y0))
    return res


def blocks(words: list[dict], k: float = 1.5) -> list[dict]:
    """Cluster nearby words (any layout: stacked lines, vertical labels) into text blocks in reading order.

    Words join a block if their boxes are within k word-heights of each other and share a rotation; reading order
    follows the rotation (0: top-to-bottom then left-to-right; 90: top-to-bottom; 270: bottom-to-top)."""
    n = len(words)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        a = words[i]
        for j in range(i + 1, n):
            b = words[j]
            if a.get("rot", 0) != b.get("rot", 0):
                continue
            lim = k * max(min(a["h"], b["h"]), 10)
            dx = max(a["x"] - (b["x"] + b["w"]), b["x"] - (a["x"] + a["w"]), 0)
            dy = max(a["y"] - (b["y"] + b["h"]), b["y"] - (a["y"] + a["h"]), 0)
            if dx <= lim and dy <= lim:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, w in enumerate(words):
        groups.setdefault(find(i), []).append(w)
    out = []
    for g in groups.values():
        rot = g[0].get("rot", 0)
        if rot == 270:
            order = sorted(g, key=lambda r: -r["y"])
        else:  # rows by vertical centre (words within half a word-height share a row), left-to-right inside a row
            rows: list[list[dict]] = []
            for r in sorted(g, key=lambda r: r["y"] + r["h"] / 2):
                yc = r["y"] + r["h"] / 2
                if rows and abs(yc - sum(q["y"] + q["h"] / 2 for q in rows[-1]) / len(rows[-1])) < 0.5 * min(r["h"], rows[-1][0]["h"]):
                    rows[-1].append(r)
                else:
                    rows.append([r])
            order = [r for row in rows for r in sorted(row, key=lambda r: r["x"])]
        x0, y0 = min(r["x"] for r in g), min(r["y"] for r in g)
        x1, y1 = max(r["x"] + r["w"] for r in g), max(r["y"] + r["h"] for r in g)
        out.append(dict(text=" ".join(r["text"] for r in order), x=x0, y=y0, w=x1 - x0, h=y1 - y0, n=len(g), words=order))
    return out


def _norm(t: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in t.lower()).split())


def find_label(words: list[dict], query: str, cutoff: float = 0.72) -> list[dict]:
    """Fuzzy search for a phrase among words, horizontal lines and windows of consecutive words in clustered blocks.

    Punctuation and case are ignored. Returns dicts with text, bounding box (x, y, w, h), kind and score, best first."""
    q = _norm(query)
    nq = len(q.split())
    cands = [dict(r, kind="word") for r in words] + [dict(r, kind="line") for r in lines(words)]
    for bl in blocks(words):
        ws = bl["words"]
        for i in range(len(ws)):
            for n in range(max(nq - 1, 1), nq + 2):
                win = ws[i:i + n]
                if len(win) < n:
                    continue
                x0, y0 = min(r["x"] for r in win), min(r["y"] for r in win)
                x1, y1 = max(r["x"] + r["w"] for r in win), max(r["y"] + r["h"] for r in win)
                cands.append(dict(text=" ".join(r["text"] for r in win), x=x0, y=y0, w=x1 - x0, h=y1 - y0, kind="block"))
    scored = []
    for r in cands:
        t = _norm(r["text"])
        if not t:
            continue
        s = difflib.SequenceMatcher(None, q, t).ratio()
        if q in t:
            s = max(s, 0.9 + 0.1 * len(q) / len(t))  # prefer the tightest container
        if s >= cutoff:
            scored.append(dict(r, score=round(s, 3)))
    return sorted(scored, key=lambda r: -r["score"])


def _lab(rgb):
    """sRGB (0-255, shape (...,3)) -> CIE Lab (D65)."""
    import numpy as np

    c = np.asarray(rgb, float) / 255
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = (c @ m.T) / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def paper_rgb(path: Path):
    """Sheet paper colour: median of the brightest 40% of a heavily downsampled copy."""
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(path).convert("RGB")
    im.thumbnail((400, 400))
    a = np.asarray(im).reshape(-1, 3)
    lum = a.sum(1)
    return tuple(int(v) for v in np.median(a[lum >= np.quantile(lum, 0.6)], axis=0))


def _name(dlab) -> str:
    """Fill name from the Lab shift (da, db) relative to paper; legend meaning comes from the sheet's key."""
    import math

    da, db = float(dlab[0]), float(dlab[1])
    if math.hypot(da, db) < 6:
        return "white/none"
    ang = math.degrees(math.atan2(db, da)) % 360  # a*: red(+)/green(-), b*: yellow(+)/blue(-)
    if ang < 25 or ang >= 335:
        return "pink/red"
    if ang < 70:
        return "orange/brown" if math.hypot(da, db) > 22 else "pink/red"
    if ang < 125:
        return "yellow"
    if ang < 200:
        return "green"
    return "blue"


def fill_at(path: Path, box, pad: int = 0, paper=None, min_de: float = 7.0):
    """Dominant fill colour in box (x, y, w, h) (+pad): median Lab shift vs paper over non-ink pixels that differ from paper.

    Returns dict(name, rgb, de, frac): frac is the share of box pixels that differ from paper (low -> uncoloured)."""
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    paper = paper or paper_rgb(path)
    x, y, w, h = box
    im = Image.open(path).convert("RGB")
    c = np.asarray(im.crop((max(x - pad, 0), max(y - pad, 0), x + w + pad, y + h + pad))).reshape(-1, 3)
    lab, p = _lab(c), _lab(np.array(paper))
    d = lab - p
    de = np.sqrt((d ** 2).sum(1))
    ink = lab[:, 0] < p[0] - 30  # dark pixels are ink, not fill
    sel = (de > min_de) & ~ink
    frac = float(sel.mean()) if len(c) else 0.0
    if sel.sum() < 30 or frac < 0.15:
        return dict(name="white/none", rgb=None, de=0.0, frac=round(frac, 3))
    med = np.median(d[sel], axis=0)
    rgb = tuple(int(v) for v in np.median(c[sel], axis=0))
    return dict(name=_name(med[1:]), rgb=rgb, de=round(float(np.hypot(*med[1:])), 1), frac=round(frac, 3))


def crop(path: Path, box, out: Path, pad: int = 80, scale: float = 1.0) -> Path:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    x, y, w, h = box
    im = Image.open(path).convert("RGB").crop((max(x - pad, 0), max(y - pad, 0), x + w + pad, y + h + pad))
    if scale != 1.0:
        im = im.resize((int(im.width * scale), int(im.height * scale)))
    im.save(out)
    return out


def cached_words(path: Path, cache: Path, **kw) -> list[dict]:
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    w = ocr_words(path, **kw)
    cache.write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    return w
