"""Experience memory: a small graph in SQLite of METHODS (not answers) learned from approved development data.

Nodes: pattern (masked question), method (operation + source kind), failure (cause), recovery (strategy text).
Edges: pattern -solved_by-> method ; failure -recovered_by-> recovery.
Seeds: verified dev runs (Train sources, approved for development), curated domain-neutral examples per operation, and
generic recovery rules. NO Test questions, Test answers or Test-derived traces are stored (rules: no Test lookups or
learned adaptation on Test outputs).
Use: similar patterns -> vote/narrow the operation menu + worked examples; failure cause -> recovery hint for the retry.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "dev" / "memory.sqlite"

# curated, domain-neutral examples (written by hand; not taken from Test questions)
CURATED = [
    ("How much did consumption in region R change from 2015 to 2020, in units and percent?", "change", "table"),
    ("Which of three districts grew most between two years?", "change", "table"),
    ("What share of the area's total in 2021 came from sector S?", "share", "table"),
    ("Compare the 2024 value with 2018 as a percentage of the earlier year.", "percent_of", "table"),
    ("Average distance per trip from the displayed distance and trip totals.", "ratio", "table"),
    ("What value does the table report for item I in 2019?", "value", "table"),
    ("How many records have a height of zero, and how many of those are not stumps?", "table_query", "csv"),
    ("Which category is most common among records on street S, and how many records does it have?", "table_query", "csv"),
    ("What is the total cost of the records of type T?", "table_query", "csv"),
    ("How many distinct labels occur in column C across the file?", "table_query", "csv"),
    ("Are there duplicate identifiers in the register?", "table_query", "csv"),
    ("How many point records are classified as ground, and what are their minimum and maximum elevation?", "table_query", "csv"),
    ("How many spaces in the building model are flagged heated?", "table_query", "csv"),
    ("What were the start and end dates of order O, and how many days elapsed?", "record_lookup", "csv"),
    ("How many records have no end date, out of how many in total?", "count_blank", "csv"),
    ("Which identifiers appear in only one of the two files?", "compare_keys", "csv"),
    ("What is the area of polygon P in hectares in the national grid?", "area", "geo"),
    ("How many trees lie within 20 metres of tree T, including it?", "spatial", "geo"),
    ("Which feature is closest to feature F, and how far away is it?", "spatial", "geo"),
    ("What area do the two crown circles of trees A and B share?", "spatial", "geo"),
    ("Which points fall outside the box between these eastings and northings?", "spatial", "geo"),
    ("How many trips run on a given date after calendar exceptions, and how many if exceptions are ignored?", "transit", "gtfs"),
    ("What is the fastest step-free walking route from entrance E to platform P?", "transit", "gtfs"),
    ("What UTC time corresponds to the scheduled arrival 25:10 on the service day?", "transit", "gtfs"),
    ("What area of the tree-cover class lies inside district D on the land-cover map?", "raster", "raster"),
    ("How much did the built-up class area change between the two map editions?", "raster", "raster"),
    ("What percentage of the boundary is covered by the map window?", "raster", "raster"),
    ("Which licence and publisher does the metadata record give?", "text_answer", "text"),
    ("What does the report say about survey response rates?", "text_answer", "text"),
    ("Can entries marked [x] be treated as zero?", "explain", "table"),
    ("Does this data show the real current status, or only a planned schedule?", "explain", "text"),
]

RECOVERY = {
    "op/source mismatch": "Workbook-table operations (value/change/share/percent_of/ratio) need a workbook table; for CSV, JSON records, "
                          "geographic layers, point clouds or building models use table_query (or record_lookup / spatial).",
    "value not found": "Use a filter value exactly as it appears in the data (see the column's top values), or a different column; "
                       "for numbers use a numeric comparison.",
    "identifier not found": "Choose the file that actually contains the identifier named in the question.",
    "bind invalid": "Fill every argument the chosen operation needs (e.g. a target column for sum/mean/min/max).",
    "format check": "When the question names file formats (CSV, GeoPackage, GeoJSON), choose one source of each named format.",
    "time cap": "Prefer a narrower filter or a smaller table; avoid grouping by high-cardinality columns.",
}

_MONTH = r"(january|february|march|april|may|june|july|august|september|october|november|december)"


def mask(q: str) -> str:
    q = re.sub(r"'[^']{1,60}'|\"[^\"]{1,60}\"", " QUOTED ", q)
    q = re.sub(r"\b[A-Za-z]*\d[\w/.:-]*\b", " ID ", q)
    q = re.sub(rf"\b{_MONTH}\b", " MONTH ", q, flags=re.I)
    q = re.sub(r"(?<=[a-z,;] )[A-Z][a-zäöüéè]+(?: [A-Z][a-zäöüéè]+)*", " NAME ", q)
    return re.sub(r"\s+", " ", q).strip()


def _toks(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 2 and w not in {"the", "and", "for", "what", "how", "many", "does"}]


def build(dev_run_files: list[Path] | None = None, db: Path = DB) -> int:
    con = sqlite3.connect(db)
    con.executescript("""DROP TABLE IF EXISTS nodes; DROP TABLE IF EXISTS edges;
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, kind TEXT, text TEXT, meta TEXT);
        CREATE TABLE edges(src INT, dst INT, rel TEXT);
        CREATE INDEX ix_nodes_kind ON nodes(kind); CREATE INDEX ix_edges_src ON edges(src, rel);""")
    meth_id = {}

    def method(op, kind):
        k = (op, kind)
        if k not in meth_id:
            meth_id[k] = con.execute("INSERT INTO nodes(kind,text,meta) VALUES('method',?,?)", (op, json.dumps({"op": op, "source_kind": kind}))).lastrowid
        return meth_id[k]

    n = 0
    for q, op, kind in CURATED:
        pid = con.execute("INSERT INTO nodes(kind,text,meta) VALUES('pattern',?,?)", (mask(q), json.dumps({"origin": "curated", "example": q}))).lastrowid
        con.execute("INSERT INTO edges VALUES(?,?,'solved_by')", (pid, method(op, kind)))
        n += 1
    for f in dev_run_files or []:
        for r in json.loads(Path(f).read_text(encoding="utf-8"))["rows"]:
            if r.get("error") or (r.get("score") is not None and r["score"] < 1.0):
                continue  # only verified-correct dev answers become memory
            op = (r.get("trace") or {}).get("plan", {}).get("op")
            if not op:
                continue
            kind = "table" if op in ("value", "change", "share", "percent_of", "ratio", "explain") else "csv" if op != "area" else "geo"
            pid = con.execute("INSERT INTO nodes(kind,text,meta) VALUES('pattern',?,?)",
                              (mask(r["question"]), json.dumps({"origin": "dev", "example": mask(r["question"])}))).lastrowid
            con.execute("INSERT INTO edges VALUES(?,?,'solved_by')", (pid, method(op, kind)))
            n += 1
    for cause, text in RECOVERY.items():
        fid = con.execute("INSERT INTO nodes(kind,text,meta) VALUES('failure',?,'{}')", (cause,)).lastrowid
        rid = con.execute("INSERT INTO nodes(kind,text,meta) VALUES('recovery',?,'{}')", (text,)).lastrowid
        con.execute("INSERT INTO edges VALUES(?,?,'recovered_by')", (fid, rid))
    con.commit()
    con.close()
    return n


class Memory:
    def __init__(self, db: Path = DB):
        self.ok = Path(db).exists()
        if not self.ok:
            return
        con = sqlite3.connect(db)
        self.rows = con.execute("""SELECT p.text, m.text, json_extract(m.meta,'$.source_kind'), json_extract(p.meta,'$.example')
                                   FROM nodes p JOIN edges e ON e.src=p.id AND e.rel='solved_by' JOIN nodes m ON m.id=e.dst
                                   WHERE p.kind='pattern'""").fetchall()
        self.rec = dict(con.execute("""SELECT f.text, r.text FROM nodes f JOIN edges e ON e.src=f.id AND e.rel='recovered_by'
                                       JOIN nodes r ON r.id=e.dst WHERE f.kind='failure'""").fetchall())
        con.close()
        self.docs = [_toks(r[0]) for r in self.rows]
        self.df = Counter(t for d in self.docs for t in set(d))
        self.avg = sum(map(len, self.docs)) / max(1, len(self.docs))

    def similar(self, question: str, k: int = 6) -> list[tuple[float, str, str, str]]:
        """(score, op, source_kind, example) for the k most similar remembered patterns (BM25 over masked text)."""
        if not self.ok:
            return []
        q, N = _toks(mask(question)), len(self.docs)
        out = []
        for d, r in zip(self.docs, self.rows):
            tf = Counter(d)
            s = sum(math.log(1 + (N - self.df[t] + 0.5) / (self.df[t] + 0.5)) * tf[t] * 2.5 / (tf[t] + 1.5 * (0.25 + 0.75 * len(d) / self.avg))
                    for t in q if t in tf)
            out.append((s, r[1], r[2], r[3]))
        return [o for o in sorted(out, key=lambda x: -x[0])[:k] if o[0] > 0]

    def recovery(self, cause: str) -> str:
        return self.rec.get(cause, "") if self.ok else ""


if __name__ == "__main__":
    import sys
    print(build([Path(p) for p in sys.argv[1:]]), "patterns stored in", DB)
