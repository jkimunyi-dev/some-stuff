"""Step 7 - retrieval over text-like sources (PDF, HTML, Markdown/TXT guides, JSON/XML metadata).

Every passage carries a verifiable locator (page, line range, JSON path, XML element) so answers can cite it.
Ranking is BM25 (implemented here; non-generative, deterministic). No pack IDs, question IDs or answer constants.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .tables import clean_name

TEXT_SUFFIXES = {".pdf", ".html", ".htm", ".md", ".txt", ".json", ".xml", ".xsd", ".csw"}
MAX_BYTES = 3_000_000  # very large JSON/XML is indexed only up to this size


@dataclass
class Passage:
    file: Path
    locator: str
    text: str

    @property
    def src_id(self) -> str:
        return self.file.name.split("__")[0]


def _chunks(lines: list[str], size: int = 14, label=lambda a, b: f"lines {a}-{b}"):
    out, buf, start = [], [], 1
    for i, ln in enumerate(lines, 1):
        if ln.strip():
            if not buf:
                start = i
            buf.append(ln.strip())
        if buf and (len(buf) >= size or (not ln.strip() and len(buf) >= 4)):
            out.append((label(start, i), " ".join(buf)))
            buf = []
    if buf:
        out.append((label(start, len(lines)), " ".join(buf)))
    return out


def _json_lines(o, path="$", out=None, limit=4000):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(o, dict):
        for k, v in o.items():
            _json_lines(v, f"{path}.{k}", out, limit)
    elif isinstance(o, list):
        for i, v in enumerate(o[:200]):
            _json_lines(v, f"{path}[{i}]", out, limit)
    else:
        out.append(f"{path}: {o}")
    return out


def passages(path: Path) -> list[Passage]:
    p, suf = Path(path), Path(path).suffix.lower()
    res: list[Passage] = []
    try:
        if suf == ".pdf":
            import pdfplumber
            with pdfplumber.open(p) as pdf:
                for n, page in enumerate(pdf.pages, 1):
                    txt = page.extract_text() or ""
                    for loc, t in _chunks(txt.splitlines(), 12, lambda a, b, n=n: f"page {n}, lines {a}-{b}"):
                        res.append(Passage(p, loc, t))
        elif suf in (".html", ".htm"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(p.read_bytes()[:MAX_BYTES], "lxml")
            for s in soup(["script", "style", "nav", "footer"]):
                s.decompose()
            blocks = []
            for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "caption", "dt", "dd"]):
                t = " ".join(el.get_text(" ", strip=True).split())
                if t:
                    blocks.append(t)
            for i in range(0, len(blocks), 8):
                res.append(Passage(p, f"HTML text blocks {i + 1}-{min(i + 8, len(blocks))}", " ".join(blocks[i:i + 8])))
        elif suf == ".json":
            raw = p.read_bytes()[:MAX_BYTES].decode("utf-8", "ignore")
            try:
                lines = _json_lines(json.loads(raw))
            except ValueError:
                lines = raw.splitlines()
            for i in range(0, len(lines), 12):
                grp = lines[i:i + 12]
                res.append(Passage(p, f"JSON {grp[0].split(':')[0]} .. {grp[-1].split(':')[0]}"[:160], " | ".join(grp)))
        elif suf in (".xml", ".xsd", ".csw"):
            import xml.etree.ElementTree as ET
            root = ET.fromstring(p.read_bytes()[:MAX_BYTES])
            lines = []
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                t = (el.text or "").strip()
                attrs = " ".join(f"{k.split('}')[-1]}={v}" for k, v in el.attrib.items())[:200]
                if t or attrs:
                    lines.append(f"<{tag}> {attrs} {t}".strip())
            for i in range(0, len(lines), 12):
                res.append(Passage(p, f"XML elements {i + 1}-{min(i + 12, len(lines))}", " | ".join(lines[i:i + 12])))
        else:  # md / txt
            txt = p.read_bytes()[:MAX_BYTES].decode("utf-8-sig", "ignore")
            for loc, t in _chunks(txt.splitlines(), 14):
                res.append(Passage(p, loc, t))
    except Exception as e:
        res.append(Passage(p, "whole file", f"(could not extract text: {type(e).__name__})"))
    return res


_TOK = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:[._/-][A-Za-z0-9]+)*")
_STOP = set("the a an of to in and or for on at by is are was were be with as that this which what how many much from it its can we our does do did".split())


def tokens(s: str) -> list[str]:
    return [t.lower() for t in _TOK.findall(s) if t.lower() not in _STOP]


class Index:
    def __init__(self, files: list[Path]):
        self.ps: list[Passage] = [x for f in files for x in passages(f)]
        self.docs = [tokens(f"{clean_name(x.file)} {x.text}") for x in self.ps]
        self.df = Counter(t for d in self.docs for t in set(d))
        self.avg = sum(map(len, self.docs)) / max(1, len(self.docs))

    def search(self, query: str, k: int = 6, k1: float = 1.5, b: float = 0.75) -> list[Passage]:
        q = tokens(query)
        N = len(self.docs)
        scores = []
        for i, d in enumerate(self.docs):
            tf = Counter(d)
            s = 0.0
            for t in q:
                if t in tf:
                    idf = math.log(1 + (N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / self.avg))
            scores.append(s)
        order = sorted(range(N), key=lambda i: -scores[i])
        return [self.ps[i] for i in order[:k] if scores[i] > 0]
