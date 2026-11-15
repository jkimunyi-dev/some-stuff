"""Record tables from specialised formats, so the general table query can answer questions about them.

  IFC (STEP text, no ifcopenshell needed): one row per IfcSpace with Name, LongName and every property-set value
      (column '<Pset>.<Property>'), plus one row per IfcBuildingStorey in a second table.
  LAS/LAZ point clouds: one row per point (X, Y, Z in file units, classification, return_number, number_of_returns,
      withheld/synthetic/keypoint flags, intensity), plus header facts (scales, extrema, point count).
No pack IDs or answer constants.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- IFC
_ENT = re.compile(r"^#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\);\s*$", re.S)


def _ifc_unescape(s: str) -> str:
    def x2(m):
        h = m.group(1)
        return "".join(chr(int(h[i:i + 4], 16)) for i in range(0, len(h), 4))
    s = re.sub(r"\\X2\\([0-9A-Fa-f]+)\\X0\\", x2, s)
    s = re.sub(r"\\X\\([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("latin-1"), s)
    return s.replace("''", "'")


def _split_args(a: str) -> list[str]:
    out, depth, cur, q = [], 0, "", False
    for ch in a:
        if ch == "'" :
            q = not q
        if not q and ch in "(":
            depth += 1
        if not q and ch in ")":
            depth -= 1
        if not q and ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def _val(tok: str):
    tok = tok.strip()
    if tok in ("$", "*"):
        return None
    if tok.startswith("'") and tok.endswith("'"):
        return _ifc_unescape(tok[1:-1])
    m = re.match(r"^[A-Z0-9_]+\((.*)\)$", tok, re.S)  # typed value e.g. IFCLABEL('x'), IFCAREAMEASURE(4.5), IFCBOOLEAN(.T.)
    if m:
        return _val(m.group(1))
    if tok in (".T.", ".F.", ".U."):
        return {".T.": "TRUE", ".F.": "FALSE", ".U.": "UNKNOWN"}[tok]
    if tok.startswith("."):
        return tok.strip(".")
    return tok


def ifc_tables(path: Path) -> dict[str, pd.DataFrame]:
    text = Path(path).read_text(encoding="latin-1", errors="ignore")
    ents = {}
    for stmt in re.split(r";\s*\n", text):
        m = _ENT.match(stmt.strip() + ");" if not stmt.strip().endswith(")") else stmt.strip() + ";")
        if not m:
            m = _ENT.match(stmt.strip() + ";")
        if m:
            ents[int(m.group(1))] = (m.group(2), m.group(3))
    props = {}  # property id -> (name, value)
    for i, (t, a) in ents.items():
        if t == "IFCPROPERTYSINGLEVALUE":
            ar = _split_args(a)
            props[i] = (_val(ar[0]), _val(ar[2]) if len(ar) > 2 else None)
    psets = {}  # pset id -> (name, {prop: value})
    for i, (t, a) in ents.items():
        if t in ("IFCPROPERTYSET", "IFCELEMENTQUANTITY"):
            ar = _split_args(a)
            ids = [int(x) for x in re.findall(r"#(\d+)", ar[4] if len(ar) > 4 else "")]
            psets[i] = (_val(ar[2]), {props[p][0]: props[p][1] for p in ids if p in props})
    attached = {}  # object id -> list of pset ids
    for t, a in ents.values():
        if t == "IFCRELDEFINESBYPROPERTIES":
            ar = _split_args(a)
            objs = [int(x) for x in re.findall(r"#(\d+)", ar[4])]
            ps = re.findall(r"#(\d+)", ar[5])
            for o in objs:
                attached.setdefault(o, []).extend(int(p) for p in ps)
    rows = {"spaces": [], "storeys": [], "project": []}
    for i, (t, a) in ents.items():
        if t not in ("IFCSPACE", "IFCBUILDINGSTOREY", "IFCPROJECT"):
            continue
        ar = _split_args(a)
        r = {"step_id": f"#{i}", "Name": _val(ar[2]) if len(ar) > 2 else None, "Description": _val(ar[3]) if len(ar) > 3 else None,
             "ObjectType": _val(ar[4]) if len(ar) > 4 else None}
        if t == "IFCSPACE":
            r["LongName"] = _val(ar[7]) if len(ar) > 7 else None
        if t == "IFCBUILDINGSTOREY":
            r["LongName"] = _val(ar[7]) if len(ar) > 7 else None
            r["Elevation"] = _val(ar[9]) if len(ar) > 9 else None
        if t == "IFCPROJECT":
            r["LongName"] = _val(ar[5]) if len(ar) > 5 else None
            r["Phase"] = _val(ar[6]) if len(ar) > 6 else None
        for p in attached.get(i, []):
            if p in psets:
                pn, d = psets[p]
                for k, v in d.items():
                    r[f"{pn}.{k}"] = v
        rows["spaces" if t == "IFCSPACE" else "storeys" if t == "IFCBUILDINGSTOREY" else "project"].append(r)
    return {k: pd.DataFrame(v) for k, v in rows.items() if v}


# ---------------------------------------------------------------- LAS / LAZ
def las_table(path: Path) -> tuple[pd.DataFrame, str]:
    import laspy
    import numpy as np
    las = laspy.read(path)
    h = las.header
    df = pd.DataFrame({"X": np.asarray(las.x), "Y": np.asarray(las.y), "Z": np.asarray(las.z, dtype="float32"),
                       "classification": np.asarray(las.classification).astype("uint8"),
                       "return_number": np.asarray(las.return_number).astype("uint8"),
                       "number_of_returns": np.asarray(las.number_of_returns).astype("uint8"),
                       "intensity": np.asarray(las.intensity).astype("uint16")})
    for flag in ("withheld", "synthetic", "key_point", "overlap"):
        try:
            df[flag] = np.asarray(getattr(las, flag)).astype("uint8")
        except Exception:
            pass
    facts = (f"point records {h.point_count}; x {h.mins[0]:.3f}..{h.maxs[0]:.3f}; y {h.mins[1]:.3f}..{h.maxs[1]:.3f}; "
             f"z {h.mins[2]:.3f}..{h.maxs[2]:.3f}; scales {tuple(h.scales)}; LAS {h.version}; point format {h.point_format.id}")
    return df, facts
