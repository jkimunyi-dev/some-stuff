"""Layer-1 deterministic dev scorer (no judge model, nothing leaves the machine).

Approximates the organiser formula element = 0.8*c + 0.2*c*e:
  c = share of the reference's numeric values found in the candidate answer (1 / 0.5 / 0),
  e = 1 if the candidate's evidence names the reference's source file.
Questions whose reference has no numbers are reported separately as `non_numeric` (need the judge / manual check).
Domain scores are averaged with equal weight across domains, as in the real scoring.
"""
from __future__ import annotations

import re
from collections import defaultdict

_NUM = re.compile(r"(?<![A-Za-z0-9_.])[-+−]?\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text.replace("−", "-")):
        raw = m.group(0).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if "." not in raw and 1900 <= abs(v) <= 2100:  # bare years are context, not results
            continue
        out.append(v)
    return out


def correctness(reference: str, candidate: str, tol: float = 0.005) -> float | None:
    ref = numbers(reference)
    if not ref:
        return None
    cand = numbers(candidate)
    hit = sum(any(abs(r - c) <= tol + 1e-9 for c in cand) for r in ref)
    frac = hit / len(ref)
    return 1.0 if frac == 1 else (0.5 if frac >= 0.5 else 0.0)


def evidence_ok(ref_evidence: list[str], cand_evidence: list[str]) -> float:
    files = {e.split(",")[0].strip().lower() for e in ref_evidence}
    blob = " ".join(cand_evidence).lower()
    return 1.0 if any(f and f in blob for f in files) else 0.0


def score_items(rows: list[dict]) -> dict:
    """rows: {domain, reference, evidence_ref, answer, evidence}. Returns overall + per-domain summary."""
    per, non_num = defaultdict(list), 0
    for r in rows:
        c = correctness(r["reference"], r["answer"])
        if c is None:
            non_num += 1
            r["score"] = None
            continue
        e = evidence_ok(r["evidence_ref"], r["evidence"])
        r["c"], r["e"] = c, e
        r["score"] = 0.8 * c + 0.2 * c * e
        per[r["domain"]].append(r["score"])
    dom = {d: sum(v) / len(v) for d, v in per.items()}
    return {"overall": sum(dom.values()) / len(dom) if dom else 0.0, "by_domain": dom,
            "n_scored": sum(len(v) for v in per.values()), "non_numeric": non_num,
            "exact": sum(1 for r in rows if r.get("c") == 1.0), "n": len(rows)}
