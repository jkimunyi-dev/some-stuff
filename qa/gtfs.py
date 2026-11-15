"""Step 4 - GTFS family tools. Generic: any feed directory of GTFS .txt files (names may carry a SRC_<id>__ prefix).

All tools return qa.tools.Result with evidence locators (file + key). No pack IDs, question IDs or answer constants.
Conventions (from Context, passed in as parameters, never hard-coded): service date, allowed pathway modes,
blank location_type == 0, times may exceed 24:00:00, distances on the WGS84 ellipsoid with inclusive radius.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pandas as pd

from .tools import Recorder, Result, geodesic_distance_m, gtfs_active_services, gtfs_time_to_seconds, pathway_shortest, round_half_up

LOCATION_TYPES = {0: "stop/platform", 1: "station", 2: "entrance/exit", 3: "generic node", 4: "boarding area"}
PATHWAY_MODES = {1: "walkway", 2: "stairs", 3: "moving sidewalk", 4: "escalator", 5: "elevator", 6: "fare gate", 7: "exit gate"}


class Feed:
    def __init__(self, folder: Path, rec: Recorder | None = None):
        self.rec = rec or Recorder()
        self.paths: dict[str, Path] = {}
        for p in Path(folder).iterdir():
            if p.suffix == ".txt":
                self.paths[re.sub(r"^SRC_[0-9a-f]+__", "", p.name)[:-4]] = p
        self._t: dict[str, pd.DataFrame] = {}

    # ---- loading: everything as str, BOM-safe, blanks stay "" (never NaN -> 0)
    def t(self, name: str) -> pd.DataFrame:
        if name not in self._t:
            self._t[name] = pd.read_csv(self.paths[name], dtype=str, keep_default_na=False, encoding="utf-8-sig")
        return self._t[name]

    def has(self, name: str) -> bool:
        return name in self.paths

    def _ev(self, name: str, key: str) -> str:
        return self.rec.add(f"{name}.txt | {key}")

    # ---- lookups
    def row(self, table: str, key_col: str, key: str) -> dict | None:
        df = self.t(table)
        m = df[df[key_col] == key]
        if m.empty:
            return None
        self._ev(table, f"{key_col}={key}")
        return m.iloc[0].to_dict()

    def find_stops(self, text: str, location_type: int | None = None) -> pd.DataFrame:
        """Case-insensitive substring match on stop_name (and exact stop_id)."""
        s = self.stops()
        m = s[s.stop_name.str.lower().str.contains(re.escape(text.lower())) | (s.stop_id == text)]
        if location_type is not None:
            m = m[m.loc_type == location_type]
        return m

    def stops(self) -> pd.DataFrame:
        s = self.t("stops").copy()
        s["loc_type"] = s.location_type.map(lambda v: 0 if v.strip() == "" else int(v))  # blank == 0
        return s

    # ---- stops.txt composition: why row count != platform count
    def stop_breakdown(self) -> Result:
        s = self.stops()
        coded = s[(s.loc_type == 0) & (s.platform_code.str.strip() != "")]
        uncoded = s[(s.loc_type == 0) & (s.platform_code.str.strip() == "")]
        counts = {"rows": len(s), "stop_or_platform_total": int((s.loc_type == 0).sum()),
                  "stop_or_platform_with_platform_code": len(coded), "stop_or_platform_without_platform_code": len(uncoded)}
        for k, name in LOCATION_TYPES.items():
            if k:
                counts[name] = int((s.loc_type == k).sum())
        assert sum(int((s.loc_type == k).sum()) for k in LOCATION_TYPES) == len(s), "unknown location_type present"
        return Result(counts, "rows", "stops.txt, blank location_type counted as 0", [self._ev("stops", "all rows, location_type/platform_code")])

    # ---- accessibility: own flag, else parent's (explicit fallback, reported)
    def wheelchair_status(self, stop_id: str) -> Result:
        s = self.stops().set_index("stop_id")
        r = s.loc[stop_id]
        own = r.wheelchair_boarding.strip()
        ev = [self._ev("stops", f"stop_id={stop_id}")]
        if own != "":
            return Result({"value": own, "inherited": False}, "flag", f"{stop_id} own value", ev)
        par = r.parent_station.strip()
        if par and par in s.index:
            ev.append(self._ev("stops", f"stop_id={par}"))
            return Result({"value": s.loc[par].wheelchair_boarding.strip(), "inherited": True, "parent": par},
                          "flag", "blank own value; parent's value shown, GTFS: blank means 'inherit/unknown', not 'inaccessible'", ev)
        return Result({"value": "", "inherited": False}, "flag", "blank, no parent: unknown", ev)

    def children_accessibility(self, station_id: str) -> Result:
        s = self.stops()
        kids = s[s.parent_station == station_id]
        ev = [self._ev("stops", f"parent_station={station_id}")]
        return Result(kids[["stop_id", "stop_name", "loc_type", "wheelchair_boarding", "platform_code"]].to_dict("records"),
                      "rows", f"children of {station_id}; station's own flag does not settle child flags", ev)

    # ---- levels
    def level_index(self, level_id: str) -> float:
        l = self.t("levels")
        self._ev("levels", f"level_id={level_id}")
        return float(l[l.level_id == level_id].level_index.iloc[0])

    def level_difference(self, level_a: str, level_b: str) -> Result:
        d = abs(self.level_index(level_a) - self.level_index(level_b))
        return Result(d, "level indices", "level_index is an ordinal index, NOT metres", [self._ev("levels", f"{level_a},{level_b}")])

    # ---- calendar
    def active_services(self, date: str, use_exceptions: bool = True) -> set:
        cal = self.t("calendar") if self.has("calendar") else pd.DataFrame(columns=["service_id", "start_date", "end_date"] + ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])
        cd = self.t("calendar_dates") if (use_exceptions and self.has("calendar_dates")) else None
        self._ev("calendar", f"date={date}")
        if cd is not None:
            self._ev("calendar_dates", f"date={date.replace('-', '')}")
        return gtfs_active_services(cal, cd, date)

    def active_trips(self, date: str, use_exceptions: bool = True, bikes_allowed: str | None = None,
                     wheelchair_accessible: str | None = None) -> Result:
        svc = self.active_services(date, use_exceptions)
        tr = self.t("trips")
        a = tr[tr.service_id.isin(svc)]
        if bikes_allowed is not None:
            a = a[a.bikes_allowed == bikes_allowed]
        if wheelchair_accessible is not None:
            a = a[a.wheelchair_accessible == wheelchair_accessible]
        return Result(len(a), "trips", f"service date {date}, exceptions {'applied' if use_exceptions else 'ignored'}", [self._ev("trips", "service_id in active set")])

    def trip_flags(self, trip_id: str) -> Result:
        r = self.row("trips", "trip_id", trip_id)
        return Result({"bikes_allowed": r["bikes_allowed"], "wheelchair_accessible": r["wheelchair_accessible"]}, "flags",
                      "GTFS: 0/blank no info, 1 yes, 2 no", list(self.rec.items[-1:]))

    # ---- agency / timezone
    def agency(self) -> Result:
        a = self.t("agency").iloc[0].to_dict()
        return Result(a, "", "agency.txt", [self._ev("agency", "row 1")])

    def to_utc(self, local_date: str, hhmmss: str, tz: str) -> Result:
        """Local service-day time (may exceed 24h) -> UTC datetime. Hours >= 24 roll into the next calendar day."""
        from zoneinfo import ZoneInfo
        secs = gtfs_time_to_seconds(hhmmss)
        # GTFS: times are measured from "noon minus 12h" of the service day (== local midnight except on DST-change days)
        noon = dt.datetime.combine(dt.date.fromisoformat(local_date), dt.time(12)).replace(tzinfo=ZoneInfo(tz))
        base = noon.astimezone(dt.timezone.utc) - dt.timedelta(hours=12)
        out = base + dt.timedelta(seconds=secs)
        return Result(out.strftime("%Y-%m-%d %H:%M:%S"), "UTC", f"{hhmmss} on service day {local_date} in {tz}", [])

    # ---- stop times / transfers
    def stop_time_gap(self, trip_in: str, trip_out: str, stop_id: str) -> Result:
        st = self.t("stop_times")
        a = st[(st.trip_id == trip_in) & (st.stop_id == stop_id)]
        d = st[(st.trip_id == trip_out) & (st.stop_id == stop_id)]
        if a.empty or d.empty:
            return Result(None, "s", "trip not found at stop", [])
        gap = gtfs_time_to_seconds(d.iloc[0].departure_time) - gtfs_time_to_seconds(a.iloc[0].arrival_time)
        return Result(gap, "seconds", f"{a.iloc[0].arrival_time} -> {d.iloc[0].departure_time}",
                      [self._ev("stop_times", f"trip_id={trip_in}, stop_id={stop_id}"), self._ev("stop_times", f"trip_id={trip_out}, stop_id={stop_id}")])

    def transfer_between_trips(self, from_trip: str, to_trip: str) -> Result:
        tf = self.t("transfers")
        m = tf[(tf.from_trip_id == from_trip) & (tf.to_trip_id == to_trip)]
        meaning = {"0": "recommended", "1": "timed", "2": "min time required", "3": "not possible", "4": "in-seat transfer", "5": "in-seat transfer not allowed"}
        recs = m.to_dict("records")
        for r in recs:
            r["meaning"] = meaning.get(r["transfer_type"], "?")
            r["min_transfer_time_blank"] = r["min_transfer_time"] == ""  # blank != 0 s
        return Result(recs, "rows", "blank min_transfer_time is absent, not zero", [self._ev("transfers", f"from_trip_id={from_trip}, to_trip_id={to_trip}")])

    # ---- pathways
    def path_route(self, src: str, dst: str, modes: set[int] | None, exclude_modes: set[int] = frozenset()) -> Result:
        pw = self.t("pathways")
        allowed = None if modes is None else set(modes)
        if exclude_modes:
            allowed = (set(range(1, 8)) if allowed is None else allowed) - set(exclude_modes)
        secs, nodes = pathway_shortest(pw, src, dst, allowed)
        legs = []
        for u, v in zip(nodes, nodes[1:]):
            m = pw[((pw.from_stop_id == u) & (pw.to_stop_id == v)) | ((pw.from_stop_id == v) & (pw.to_stop_id == u) & (pw.is_bidirectional == "1"))]
            best = m.assign(tt=m.traversal_time.map(lambda x: float(x or 0))).sort_values("tt").iloc[0]
            legs.append({"from": u, "to": v, "mode": int(best.pathway_mode), "seconds": best.tt, "pathway_id": best.pathway_id})
            self._ev("pathways", f"pathway_id={best.pathway_id}")
        return Result({"seconds": secs, "reachable": secs is not None, "legs": legs}, "seconds",
                      f"modes {sorted(allowed) if allowed else 'all'}; recorded traversal times only", list(self.rec.items[-max(len(legs), 1):]))

    def legs_with_mode(self, legs: list[dict], mode: int) -> list[dict]:
        return [l for l in legs if l["mode"] == mode]

    # ---- geometry
    def nearest(self, target_stop_id: str, candidate_loc_type: int, within_parent: str | None = None, radius_m: float | None = None) -> Result:
        s = self.stops().set_index("stop_id")
        t = s.loc[target_stop_id]
        c = s[s.loc_type == candidate_loc_type]
        if within_parent:
            c = c[c.parent_station == within_parent]
        best = None
        for sid, r in c.iterrows():
            d = geodesic_distance_m(float(t.stop_lon), float(t.stop_lat), float(r.stop_lon), float(r.stop_lat))
            if radius_m is not None and d > radius_m:  # boundary inclusive
                continue
            if best is None or d < best[1]:
                best = (sid, d)
        if best is None:
            return Result(None, "m", "no candidate", [])
        return Result({"stop_id": best[0], "name": s.loc[best[0]].stop_name, "metres": round_half_up(best[1])}, "m",
                      "WGS84 ellipsoid, straight line, rounded half-up 2 dp", [self._ev("stops", f"stop_id={target_stop_id}"), self._ev("stops", f"stop_id={best[0]}")])

    def within_radius(self, lon: float, lat: float, radius_m: float, loc_type: int | None = None) -> Result:
        s = self.stops()
        if loc_type is not None:
            s = s[s.loc_type == loc_type]
        d = s.apply(lambda r: geodesic_distance_m(lon, lat, float(r.stop_lon), float(r.stop_lat)), axis=1)
        m = s[d <= radius_m]  # inclusive boundary
        return Result(len(m), "stops", f"WGS84 ellipsoid, radius {radius_m} m inclusive", [self._ev("stops", "stop_lat/stop_lon all rows")])
