"""Transit (GTFS) operation, wrapping the tested qa.gtfs.Feed tools.

Tasks: active_trips, route_time, nearest_stop, trip_time_at_stop, accessibility.
Stops/trips are offered from the feed (names or ids that appear in the question/Context); the step-free mode set is parsed
from the Context ('permits modes 1, 5, 6 and 7'); dates are ISO strings validated by code. No pack IDs or constants.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .gtfs import LOCATION_TYPES, Feed
from .tools import Recorder, gtfs_time_to_seconds, round_half_up

TASKS = ["active_trips", "route_time", "nearest_stop", "trip_time_at_stop", "accessibility"]
MODELS = ["unrestricted", "step_free", "step_free_without_lifts"]
_FEEDS: dict = {}


class TransitError(Exception):
    pass


def feed_for(files: list[Path]) -> Feed | None:
    folder = {f.parent for f in files if re.search(r"__(stops|trips|stop_times)\.txt$", f.name)}
    if not folder:
        return None
    names = {re.sub(r"^SRC_[0-9a-f]+__", "", f.name) for f in files}
    if not {"stops.txt", "trips.txt"} <= names:
        return None
    key = tuple(sorted(str(f) for f in files))
    if key not in _FEEDS:
        fd = Feed(next(iter(folder)))
        # restrict to this pack's files (the folder holds every source)
        fd.paths = {re.sub(r"^SRC_[0-9a-f]+__", "", f.name)[:-4]: f for f in files if f.suffix == ".txt"}
        _FEEDS[key] = fd
    return _FEEDS[key]


def step_free_modes(context: str) -> set[int] | None:
    m = re.search(r"step-?free[^.]*?modes? ([\d,\sand]+)", context, re.I)
    return {int(x) for x in re.findall(r"\d", m.group(1))} if m else None


def stop_options(fd: Feed, text: str, limit: int = 30) -> list[str]:
    """Stops ranked by IDF-weighted overlap of name words with the text (rare names like a station's dominate common words
    like 'Rail Replacement Bus Stop'); stop ids quoted verbatim come first."""
    import math
    s = fd.stops()
    tok = lambda x: {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']+", str(x)) if len(w) > 1}
    names = [tok(n) for n in s.stop_name]
    df = {}
    for ws in names:
        for w in ws:
            df[w] = df.get(w, 0) + 1
    N = len(names)
    words = tok(text)
    ids = set(re.findall(r"[\w:.-]+", text))
    scored = []
    for r, ws in zip(s.itertuples(), names):
        sc = sum(math.log(N / df[w]) for w in ws & words)
        if r.stop_id in ids:
            sc += 100
        if sc > math.log(N / max(1, N // 50)):  # at least one reasonably rare word (or a verbatim id)
            scored.append((sc, f"{r.stop_id} | {r.stop_name} | {LOCATION_TYPES.get(r.loc_type, r.loc_type)}"
                           + (f" | platform {r.platform_code}" if str(r.platform_code).strip() else "")))
    scored.sort(key=lambda x: -x[0])
    return [o for _, o in scored[:limit]]


def trip_options(fd: Feed, text: str) -> list[str]:
    tr = fd.t("trips")
    return [t for t in tr.trip_id.unique() if t in text][:10]


def _sid(opt: str) -> str:
    return opt.split(" | ")[0]


def run_transit(b: dict, fd: Feed, context: str, rec: Recorder):
    task = b["task"]
    facts, keys = [], []
    fd.rec = rec
    if task == "active_trips":
        d = b.get("date") or ""
        try:
            dt.date.fromisoformat(d)
        except ValueError:
            raise TransitError("date must be YYYY-MM-DD")
        flag = b.get("flag") or ""
        kw = {"bikes_allowed": "2"} if flag == "bikes_prohibited" else {"bikes_allowed": "1"} if flag == "bikes_allowed" else \
             {"wheelchair_accessible": "1"} if flag == "wheelchair_accessible" else {}
        a = fd.active_trips(d, True, **kw).value
        n = fd.active_trips(d, False, **kw).value
        tot = len(fd.t("trips"))
        lab = f" with {flag}" if flag else ""
        facts.append(f"Trips active on {d}{lab}: {a} after calendar exceptions; {n} if exceptions are ignored; trips.txt has {tot} trip rows in total")
        keys += [str(a), str(n)]
    elif task == "route_time":
        src, dst = _sid(b.get("stop_a") or ""), _sid(b.get("stop_b") or "")
        if not src or not dst:
            raise TransitError("route_time needs stop_a and stop_b")
        sf = step_free_modes(context) or {1, 5, 6, 7}
        for model in b.get("models") or ["step_free"]:
            modes = None if model == "unrestricted" else sf
            excl = {5} if model == "step_free_without_lifts" else set()
            for a_, z_ in ([(src, dst), (dst, src)] if b.get("both_directions") else [(src, dst)]):
                r = fd.path_route(a_, z_, modes, exclude_modes=excl).value
                if r["reachable"]:
                    legs = "; ".join(f"{l['from']}->{l['to']} mode {l['mode']} {l['seconds']:g}s" for l in r["legs"][:8])
                    facts.append(f"{model} route {a_} -> {z_}: {r['seconds']:g} s ({legs})")
                    keys.append(f"{r['seconds']:g}")
                else:
                    facts.append(f"{model} route {a_} -> {z_}: no route in the supplied pathways")
    elif task == "nearest_stop":
        tgt = _sid(b.get("stop_a") or "")
        lt = {v: k for k, v in LOCATION_TYPES.items()}.get(b.get("candidate_type") or "entrance/exit", 2)
        par = fd.stops().set_index("stop_id").loc[tgt].parent_station if b.get("same_station") else None
        r = fd.nearest(tgt, lt, within_parent=par or None).value
        if not r:
            raise TransitError("no candidate of that type")
        facts.append(f"Nearest {b.get('candidate_type')} to {tgt}: {r['stop_id']} ({r['name']}) at {r['metres']:.2f} m (WGS84 ellipsoid)")
        keys.append(f"{r['metres']:.2f}")
    elif task == "trip_time_at_stop":
        trip, stop = b.get("trip") or "", _sid(b.get("stop_a") or "")
        st = fd.t("stop_times")
        m = st[(st.trip_id == trip) & ((st.stop_id == stop) if stop else True)]
        if m.empty:
            raise TransitError("trip/stop not found in stop_times")
        row = m.iloc[-1] if b.get("which") == "last" or not stop else m.iloc[0]
        tm = row.arrival_time
        d = b.get("date") or ""
        tz = fd.t("agency").iloc[0].get("agency_timezone", "")
        secs = gtfs_time_to_seconds(tm)
        first = gtfs_time_to_seconds(st[st.trip_id == trip].iloc[0].departure_time)
        facts.append(f"Trip {trip} at stop {row.stop_id}: arrival {tm} (service-day time); trip runs {secs - first} s from its first departure")
        keys.append(str(secs - first))
        if d:
            day = dt.date.fromisoformat(d) + dt.timedelta(days=secs // 86400)
            hh, rem = divmod(secs % 86400, 3600)
            facts.append(f"On service day {d} that is {day.isoformat()} {hh:02d}:{rem // 60:02d}:{rem % 60:02d} local ({tz})")
            if tz:
                facts.append(f"In UTC: {fd.to_utc(d, tm, tz).value}")
        rec.add(f"stop_times.txt | trip_id={trip}, stop_id={row.stop_id}")
    elif task == "accessibility":
        stop = _sid(b.get("stop_a") or "")
        w = fd.wheelchair_status(stop).value
        facts.append(f"{stop} wheelchair_boarding own value '{w['value'] if not w.get('inherited') else ''}'" +
                     (f"; blank, parent {w['parent']} has '{w['value']}'" if w.get("inherited") else ""))
        kids = fd.children_accessibility(stop).value
        for k in kids[:10]:
            facts.append(f"  child {k['stop_id']} ({k['stop_name']}, type {k['loc_type']}): wheelchair_boarding '{k['wheelchair_boarding']}'")
    return facts, keys
