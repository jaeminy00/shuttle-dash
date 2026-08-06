#!/usr/bin/env python3
"""
shuttle_dash.py - multi-agency shuttle/bus departure board.

Fetches GTFS static feeds (schedules) and GTFS-realtime feeds (live
predictions) from one or more sources (LBNL TripShot, AC Transit, ...),
computes upcoming departures for the boards in config.json, and serves a
glanceable dashboard on a local web page.

Usage:
    python3 shuttle_dash.py                     # live mode (fetches feeds)
    python3 shuttle_dash.py --offline DIR       # offline test mode; DIR holds
                                                #   <source>_gtfs.zip and <source>_rt.pb
    python3 shuttle_dash.py --now "2026-08-04 08:00"    # freeze the clock
    python3 shuttle_dash.py --find-stops "hearst"       # search stops in all sources

Dependencies: gtfs-realtime-bindings  (pip install gtfs-realtime-bindings)
"""

import argparse
import csv
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "shuttle-dash/2.0"

# ---------------------------------------------------------------- config

def load_config():
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        cfg = json.load(f)
    cfg["cache_dir"] = os.path.expanduser(cfg.get("cache_dir", "~/.cache/shuttle-dash"))
    os.makedirs(cfg["cache_dir"], exist_ok=True)
    return cfg


def http_get(url, timeout=30):
    """Fetch a URL; fall back to the system curl if Python's request fails.

    Some feed servers (api.bart.gov) reject Python's HTTP requests at the
    WAF level with misleading application errors while accepting identical
    requests from curl, so curl is the fallback of last resort.
    """
    def gunzip(data):
        # some servers (e.g. 511.org) gzip unconditionally
        return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data

    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0",
                                               "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return gunzip(resp.read())
    except Exception as ex:
        if not shutil.which("curl"):
            raise
        r = subprocess.run(
            ["curl", "-sS", "--compressed", "--fail-with-body",
             "--max-time", str(int(timeout)), url],
            capture_output=True)
        if r.returncode == 0:
            return gunzip(r.stdout)
        raise RuntimeError(
            f"python fetch failed ({ex}); curl fallback also failed: "
            f"{(r.stdout or r.stderr)[:200]!r}") from ex

# ---------------------------------------------------------------- static GTFS

class StaticGTFS:
    """Parsed subset of one GTFS static feed, filtered to this source's boards.

    Also resolves each board's `route` (short or long name) and `stop`
    (stop_id, stop_code, exact name, or unique name fragment among the
    stops that route actually serves).
    """

    def __init__(self, zip_bytes, tz, boards):
        self.tz = tz
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = set(z.namelist())

        def rows(name):
            if name not in names:
                return
            with z.open(name) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))

        # ---- routes: resolve each board's route name -> set of route_ids
        route_info = {}   # route_id -> (short, long, color)
        for r in rows("routes.txt"):
            route_info[r["route_id"]] = (r.get("route_short_name", ""),
                                         r.get("route_long_name", ""),
                                         r.get("route_color", ""))
        self.boards = []
        for b in boards:
            q = b["route"].strip().lower()
            # exact short/long name first; then substring of either (so one
            # fragment can catch e.g. both direction-variants of a BART line)
            rids = {rid for rid, (s, l, c) in route_info.items()
                    if s.strip().lower() == q or l.strip().lower() == q}
            if not rids and len(q) >= 2:
                rids = {rid for rid, (s, l, c) in route_info.items()
                        if q in s.lower() or q in l.lower()}
            color = next(( "#" + route_info[rid][2] for rid in rids
                           if route_info[rid][2]), "#3987e5")
            board = dict(b)
            board["route_ids"] = rids
            board["route_color"] = color
            board["error"] = None
            if not rids:
                avail = sorted({f"{s or l}" for s, l, c in route_info.values()})
                board["error"] = (f"route '{b['route']}' not found; "
                                  f"available: {', '.join(avail[:25])}")
            self.boards.append(board)

        # ---- stops: candidate stop_ids per board
        stops = {}   # stop_id -> (code, name)
        for r in rows("stops.txt"):
            stops[r["stop_id"]] = (r.get("stop_code", ""), r.get("stop_name", ""))
        self.stop_names = {sid: nm for sid, (cd, nm) in stops.items()}
        self.route_longname = {rid: (l or s) for rid, (s, l, c) in route_info.items()}

        def candidates(q):
            ql = q.strip().lower()
            by_id = [sid for sid in stops if sid == q]
            if by_id:
                return by_id
            by_code = [sid for sid, (cd, nm) in stops.items() if cd == q]
            if by_code:
                return by_code
            exact = [sid for sid, (cd, nm) in stops.items() if nm.strip().lower() == ql]
            if exact:
                return exact
            return [sid for sid, (cd, nm) in stops.items() if ql in nm.lower()]

        for board in self.boards:
            # `stop` may be a single query or a list of alternatives tried in
            # order (e.g. an exact id first, a name fragment as fallback).
            # Candidates are collected for ALL alternatives; the final pick
            # (after the service scan below) walks them in order, so a query
            # that matches a stop the route/direction never serves falls
            # through to the next alternative.
            queries = board["stop"] if isinstance(board["stop"], list) else [board["stop"]]
            board["stop_queries"] = queries
            board["cand_by_query"] = {q: set(candidates(q)) for q in queries}
            board["stop_candidates"] = set().union(*board["cand_by_query"].values())
            if not board["error"] and not board["stop_candidates"]:
                board["error"] = f"no stop matches {' / '.join(repr(q) for q in queries)}"

        # ---- trips for all wanted routes
        wanted_routes = set().union(*(b["route_ids"] for b in self.boards)) \
            if self.boards else set()
        self.trip_service = {}
        self.trip_route = {}
        self.trip_headsign = {}
        for r in rows("trips.txt"):
            if r["route_id"] in wanted_routes:
                tid = r["trip_id"]
                self.trip_service[tid] = r["service_id"]
                self.trip_route[tid] = r["route_id"]
                self.trip_headsign[tid] = r.get("trip_headsign", "")

        # ---- stop_times at candidate stops for wanted trips
        all_candidates = set().union(*(b["stop_candidates"] for b in self.boards)) \
            if self.boards else set()
        self.stop_dep = {}   # (trip_id, stop_id) -> seconds since service midnight
        self.route_stops = {}  # route_id -> every stop_id its trips serve
        served = {}          # board idx -> set of stop_ids actually served by its route
        for r in rows("stop_times.txt"):
            tid, sid = r["trip_id"], r["stop_id"]
            if tid not in self.trip_route:
                continue
            self.route_stops.setdefault(self.trip_route[tid], set()).add(sid)
            if sid not in all_candidates:
                continue
            if r.get("pickup_type", "0") == "1":
                continue  # no pickup here
            t = r["departure_time"] or r["arrival_time"]
            if not t:
                continue
            h, m, s = (int(x) for x in t.split(":"))
            self.stop_dep[(tid, sid)] = h * 3600 + m * 60 + s
            for i, board in enumerate(self.boards):
                if self.trip_route[tid] not in board["route_ids"]:
                    continue
                if sid not in board["stop_candidates"]:
                    continue
                # respect the board's direction filter during resolution, so
                # e.g. a BART platform pair disambiguates by travel direction
                dir_filter = [x.lower() for x in board.get("direction_contains", [])]
                if dir_filter:
                    hs = self.trip_headsign.get(tid, "").lower()
                    if not any(f in hs for f in dir_filter):
                        continue
                served.setdefault(i, set()).add(sid)

        # ---- disambiguate each board's stop
        for i, board in enumerate(self.boards):
            if board["error"]:
                continue
            hits = served.get(i, set())
            resolved = False
            for q in board["stop_queries"]:
                q_hits = hits & board["cand_by_query"][q]
                if not q_hits:
                    continue
                if len(q_hits) == 1:
                    board["stop_id"] = next(iter(q_hits))
                    board["stop_resolved_name"] = stops[board["stop_id"]][1]
                    code = stops[board["stop_id"]][0]
                    print(f"[{board['title']}] stop '{q}' -> "
                          f"{board['stop_resolved_name']} "
                          f"(id {board['stop_id']}{', code ' + code if code else ''})")
                else:
                    cand = sorted(f"{stops[s][1]} (code {stops[s][0] or s})" for s in q_hits)
                    board["error"] = (f"'{q}' is ambiguous for route "
                                      f"'{board['route']}' — it serves: {'; '.join(cand)}. "
                                      f"Put the stop code in config.json.")
                resolved = True
                break
            if not resolved:
                cand = sorted(f"{stops[s][1]} (code {stops[s][0] or s})"
                              for s in board["stop_candidates"])[:12]
                board["error"] = (f"route '{board['route']}' does not serve any stop "
                                  f"matching {' / '.join(repr(q) for q in board['stop_queries'])}"
                                  f"{' in this direction' if board.get('direction_contains') else ''}. "
                                  f"Stops matching the text: {'; '.join(cand)}. "
                                  f"Try --find-stops to locate the right one.")

        # ---- calendar
        self.calendar = list(rows("calendar.txt"))
        self.calendar_dates = {}
        for r in rows("calendar_dates.txt"):
            self.calendar_dates[(r["service_id"], r["date"])] = r["exception_type"]

    def services_on(self, d: date):
        ymd = d.strftime("%Y%m%d")
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        active = set()
        for r in self.calendar:
            if r[days[d.weekday()]] == "1" and r["start_date"] <= ymd <= r["end_date"]:
                active.add(r["service_id"])
        for (sid, day), ex in self.calendar_dates.items():
            if day == ymd:
                (active.add if ex == "1" else active.discard)(sid)
        return active

    def local_midnight_epoch(self, d: date):
        # DST-safe "noon minus 12h" GTFS convention
        noon = datetime(d.year, d.month, d.day, 12, tzinfo=self.tz)
        return (noon - timedelta(hours=12)).timestamp()

    def departures(self, board, now_epoch, horizon_h=26):
        """Scheduled departures for one resolved board, sorted by time."""
        out = []
        if board.get("error") or "stop_id" not in board:
            return out
        dir_filter = [s.lower() for s in board.get("direction_contains", [])]
        today = datetime.fromtimestamp(now_epoch, self.tz).date()
        for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
            active = self.services_on(d)
            base = self.local_midnight_epoch(d)
            for (trip_id, stop_id), secs in self.stop_dep.items():
                if stop_id != board["stop_id"]:
                    continue
                if self.trip_route[trip_id] not in board["route_ids"]:
                    continue
                if self.trip_service[trip_id] not in active:
                    continue
                if dir_filter:
                    hs = self.trip_headsign.get(trip_id, "").lower()
                    if not any(f in hs for f in dir_filter):
                        continue
                t = base + secs
                if now_epoch - 30 * 60 < t < now_epoch + horizon_h * 3600:
                    out.append({"trip_id": trip_id, "sched": t,
                                "headsign": self.trip_headsign.get(trip_id, "")})
        out.sort(key=lambda x: x["sched"])
        return out

# ---------------------------------------------------------------- realtime

class RealtimeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.updates = {}        # trip_id -> {stop_id: {"time","delay","skipped"}}
        self.cancelled = set()
        self.alerts = []
        self.fetched_at = None
        self.error = None

    def ingest(self, pb_bytes, when):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(pb_bytes)
        updates, cancelled, alerts = {}, set(), []
        for e in feed.entity:
            if e.HasField("trip_update"):
                tu = e.trip_update
                tid = tu.trip.trip_id
                if tu.trip.schedule_relationship == tu.trip.CANCELED:
                    cancelled.add(tid)
                    continue
                stu_map = {}
                last_stop = None
                for stu in tu.stop_time_update:
                    skipped = stu.schedule_relationship == stu.SKIPPED
                    t = None
                    if stu.HasField("departure") and stu.departure.time:
                        t = stu.departure.time
                    elif stu.HasField("arrival") and stu.arrival.time:
                        t = stu.arrival.time
                    stu_map[stu.stop_id] = {"time": t, "skipped": skipped}
                    if not skipped:
                        last_stop = stu.stop_id
                if stu_map:
                    updates[tid] = {"route_id": tu.trip.route_id,
                                    "stops": stu_map, "last_stop": last_stop}
            elif e.HasField("alert"):
                a = e.alert
                text = ""
                if a.header_text.translation:
                    text = a.header_text.translation[0].text
                if a.description_text.translation:
                    desc = a.description_text.translation[0].text
                    if desc and desc != text:
                        text = f"{text} — {desc}" if text else desc
                alerts.append({
                    "text": text,
                    "routes": {ie.route_id for ie in a.informed_entity if ie.route_id},
                    "stops": {ie.stop_id for ie in a.informed_entity if ie.stop_id},
                })
        with self.lock:
            self.updates, self.cancelled, self.alerts = updates, cancelled, alerts
            self.fetched_at, self.error = when, None

# ---------------------------------------------------------------- source

class Source:
    """One feed provider (static schedule + realtime), shared by its boards."""

    def __init__(self, name, scfg, cfg, args, tz):
        self.name = name
        self.cfg = cfg
        self.args = args
        self.tz = tz
        token = scfg.get("token", "")
        self.static_url = scfg["gtfs_static_url"].replace("{token}", token)
        self.rt_url = scfg.get("gtfs_rt_url", "").replace("{token}", token)
        self.boards_cfg = [b for b in cfg["boards"] if b["source"] == name]
        self.poll_seconds = scfg.get("poll_seconds", cfg.get("rt_poll_seconds", 20))
        self.static = None            # StaticGTFS
        self.static_loaded_at = None
        self.static_error = None
        self.rt = RealtimeState()
        self.lock = threading.Lock()

    def _offline_path(self, suffix):
        return os.path.join(self.args.offline, f"{self.name}_{suffix}")

    def load_static(self):
        try:
            if self.args.offline:
                with open(self._offline_path("gtfs.zip"), "rb") as f:
                    data = f.read()
            else:
                cache = os.path.join(self.cfg["cache_dir"], f"gtfs_{self.name}.zip")
                data = None
                max_age = self.cfg.get("static_refresh_hours", 24) * 3600
                if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < max_age:
                    with open(cache, "rb") as f:
                        data = f.read()
                if data is None:
                    data = http_get(self.static_url, timeout=120)
                    with open(cache, "wb") as f:
                        f.write(data)
            parsed = StaticGTFS(data, self.tz, self.boards_cfg)
            with self.lock:
                self.static = parsed
                self.static_loaded_at = time.time()
                self.static_error = None
            print(f"[static:{self.name}] loaded: {len(parsed.stop_dep)} stop-times, "
                  f"{len(parsed.boards)} board(s)")
        except Exception as ex:
            with self.lock:
                self.static_error = str(ex)
            print(f"[static:{self.name}] load failed: {ex}", file=sys.stderr)

    def load_realtime(self, now):
        if not self.rt_url:
            return
        try:
            if self.args.offline:
                with open(self._offline_path("rt.pb"), "rb") as f:
                    data = f.read()
            else:
                data = http_get(self.rt_url, timeout=20)
            self.rt.ingest(data, now)
        except Exception as ex:
            with self.rt.lock:
                self.rt.error = str(ex)
            print(f"[rt:{self.name}] fetch failed: {ex}", file=sys.stderr)

class TimetableSource:
    """Fixed printed timetable — for agencies with no public feed (Bear
    Transit). Boards on this source carry their own departure "times"
    (["HH:MM", ...], 24h local) and optional "days" ("weekdays"/"daily").
    """

    def __init__(self, name, scfg, cfg, args, tz):
        self.name = name
        self.cfg = cfg
        self.args = args
        self.tz = tz
        self.poll_seconds = 3600          # nothing to poll
        self.rt = RealtimeState()
        self.static = True
        self.static_error = None
        self.static_loaded_at = time.time()
        self.lock = threading.Lock()

    def load_static(self):
        print(f"[static:{self.name}] fixed timetable source (no feed)")

    def load_realtime(self, now):
        pass


class EtdSource:
    """BART real-time departures via the legacy ETD API (api.bart.gov).

    Purpose-built for station departure boards: every estimate carries the
    destination, minutes, direction, line color, and delay — no schedule
    matching required, and it is immune to GTFS schedule-change gaps.
    Boards on this source filter with "color" (e.g. "RED") and
    "direction" ("South"/"North") instead of route/stop.
    """

    DEFAULT_URL = ("https://api.bart.gov/api/etd.aspx"
                   "?cmd=etd&orig={station}&key={token}&json=y")

    def __init__(self, name, scfg, cfg, args, tz):
        self.name = name
        self.cfg = cfg
        self.args = args
        self.tz = tz
        url = scfg.get("etd_url", self.DEFAULT_URL)
        token = scfg.get("token", "")
        if not token:  # empty token -> drop the key parameter entirely
            url = url.replace("key={token}&", "").replace("&key={token}", "")
        self.url = (url.replace("{token}", token)
                       .replace("{station}", scfg.get("station", "ALL")))
        self.station_name = scfg.get("station", "")
        self.poll_seconds = scfg.get("poll_seconds", cfg.get("rt_poll_seconds", 20))
        self.estimates = []          # guarded by self.rt.lock
        self.rt = RealtimeState()    # reused for lock / fetched_at / error
        self.static = True           # nothing to load; keep Source interface
        self.static_error = None
        self.static_loaded_at = time.time()
        self.lock = threading.Lock()

    def load_static(self):
        print(f"[static:{self.name}] ETD source — no schedule to load")
        print(f"[etd:{self.name}] polling: {self.url}")

    def load_realtime(self, now):
        try:
            if self.args.offline:
                with open(os.path.join(self.args.offline, f"{self.name}_etd.json")) as f:
                    doc = json.load(f)
            else:
                doc = json.loads(http_get(self.url, timeout=20).decode("utf-8-sig"))
            ests, station_name = [], ""
            for stn in doc.get("root", {}).get("station", []):
                station_name = stn.get("name", "")
                for etd in stn.get("etd", []):
                    dest = etd.get("destination", "")
                    for est in etd.get("estimate", []):
                        m = str(est.get("minutes", "")).strip()
                        mins = 0 if m.lower() == "leaving" else int(m)
                        ests.append({
                            "dest": dest,
                            "time": now + mins * 60,
                            "direction": est.get("direction", ""),
                            "color": est.get("color", ""),
                            "hexcolor": est.get("hexcolor", ""),
                            "delay": int(est.get("delay", 0) or 0),
                            "cancelled": str(est.get("cancelflag", "0")) == "1",
                        })
            with self.rt.lock:
                self.estimates = ests
                if station_name:
                    self.station_name = station_name
                self.rt.fetched_at = now
                self.rt.error = None
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", "replace").strip()[:300]
            except Exception:
                pass
            msg = f"HTTP {ex.code}: {body or ex.reason}"
            with self.rt.lock:
                self.rt.error = msg
            print(f"[etd:{self.name}] fetch failed: {msg}", file=sys.stderr)
        except Exception as ex:
            with self.rt.lock:
                self.rt.error = str(ex)
            print(f"[etd:{self.name}] fetch failed: {ex}", file=sys.stderr)

# ---------------------------------------------------------------- app

class App:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.tz = ZoneInfo(cfg["timezone"])
        self.sources = {}
        for name, scfg in cfg["sources"].items():
            if not any(b["source"] == name for b in cfg["boards"]):
                continue
            cls = {"bart_etd": EtdSource,
                   "timetable": TimetableSource}.get(scfg.get("type"), Source)
            self.sources[name] = cls(name, scfg, cfg, args, self.tz)

    def now(self):
        if self.args.now:
            return self._fake_now
        return time.time()

    def start(self):
        for src in self.sources.values():
            src.load_static()
            src.load_realtime(self.now())
        if not self.args.offline:
            for src in self.sources.values():
                threading.Thread(target=self._poll_source, args=(src,),
                                 daemon=True).start()
            threading.Thread(target=self._static_refresher, daemon=True).start()

    def _poll_source(self, src):
        # each source has its own cadence (rate-limited APIs poll slower)
        while True:
            time.sleep(src.poll_seconds)
            src.load_realtime(self.now())

    def _static_refresher(self):
        while True:
            time.sleep(3600)
            for src in self.sources.values():
                age = time.time() - (src.static_loaded_at or 0)
                if age >= self.cfg.get("static_refresh_hours", 24) * 3600:
                    src.load_static()

    def board_json(self):
        now = self.now()
        out = {"now": now, "timezone": self.cfg["timezone"], "boards": [],
               "sources": {}, "default_tab": self.cfg.get("default_tab", "work")}
        n_shown = self.cfg.get("departures_shown", 3)

        for name, src in self.sources.items():
            with src.rt.lock:
                out["sources"][name] = {
                    "rt_age": (round(now - src.rt.fetched_at)
                               if src.rt.fetched_at else None),
                    "rt_error": src.rt.error,
                    "static_error": src.static_error,
                    "poll_seconds": src.poll_seconds,
                }

        for bcfg in self.cfg["boards"]:
            src = self.sources[bcfg["source"]]
            bstop = bcfg.get("stop", "")
            entry = {
                "title": bcfg["title"],
                "tab": bcfg.get("tab", "work"),
                "source": bcfg["source"],
                "stop": " / ".join(bstop) if isinstance(bstop, list) else bstop,
                "walk_minutes": bcfg.get("walk_minutes", 10),
                "route_color": "#3987e5",
                "departures": [],
                "alerts": [],
                "error": None,
            }
            color_override = bcfg.get("route_color")

            # ---- printed-timetable board (times live in the board config)
            if isinstance(src, TimetableSource):
                days = bcfg.get("days", "weekdays")
                deps = []
                today = datetime.fromtimestamp(now, self.tz).date()
                for d in (today - timedelta(days=1), today,
                          today + timedelta(days=1)):
                    if days == "weekdays" and d.weekday() >= 5:
                        continue
                    noon = datetime(d.year, d.month, d.day, 12, tzinfo=self.tz)
                    base = (noon - timedelta(hours=12)).timestamp()
                    for hm in bcfg.get("times", []):
                        h, m = (int(x) for x in hm.split(":"))
                        t = base + h * 3600 + m * 60
                        if now - 30 <= t < now + 26 * 3600:
                            deps.append({"sched": t, "time": t,
                                         "headsign": bcfg.get("headsign", ""),
                                         "realtime": False, "delay": 0,
                                         "cancelled": False})
                entry["departures"] = sorted(deps, key=lambda x: x["time"])[:n_shown]
                if color_override:
                    entry["route_color"] = color_override
                out["boards"].append(entry)
                continue

            # ---- ETD-backed board (filter live estimates by color+direction)
            if isinstance(src, EtdSource):
                with src.rt.lock:
                    ests = list(src.estimates)
                    stn = src.station_name
                    rt_err = src.rt.error
                entry["stop"] = stn or entry["stop"]
                colorq = bcfg.get("color", "").strip().lower()
                dirq = bcfg.get("direction", "").strip().lower()
                deps = []
                for e in ests:
                    if colorq and e["color"].lower() != colorq:
                        continue
                    if dirq and e["direction"].lower() != dirq:
                        continue
                    if e["hexcolor"]:
                        entry["route_color"] = e["hexcolor"]
                    deps.append({
                        "sched": e["time"] - e["delay"],
                        "time": e["time"],
                        "headsign": e["dest"],
                        "realtime": True,
                        "delay": e["delay"],
                        "cancelled": e["cancelled"],
                    })
                entry["departures"] = sorted(
                    [x for x in deps if x["time"] >= now - 30],
                    key=lambda x: x["time"])[:n_shown]
                if not ests and rt_err:
                    entry["error"] = f"ETD fetch failed: {rt_err}"
                if color_override:
                    entry["route_color"] = color_override
                out["boards"].append(entry)
                continue
            with src.lock:
                st = src.static
            if st is None:
                entry["error"] = src.static_error or "schedule not loaded yet"
                out["boards"].append(entry)
                continue
            board = next((b for b in st.boards if b["title"] == bcfg["title"]), None)
            if board is None or board.get("error"):
                entry["error"] = board["error"] if board else "board not found"
                out["boards"].append(entry)
                continue

            entry["route_color"] = color_override or board["route_color"]
            entry["stop"] = board.get("stop_resolved_name", entry["stop"])

            with src.rt.lock:
                rt_updates = dict(src.rt.updates)
                rt_cancelled = set(src.rt.cancelled)
                rt_alerts = list(src.rt.alerts)

            deps = []
            sched_tids = set()
            for d in st.departures(board, now):
                sched_tids.add(d["trip_id"])
                item = {
                    "sched": d["sched"],
                    "time": d["sched"],
                    "headsign": d["headsign"],
                    "realtime": False,
                    "delay": 0,
                    "cancelled": d["trip_id"] in rt_cancelled,
                }
                stu = rt_updates.get(d["trip_id"], {}).get("stops", {}) \
                                .get(board["stop_id"])
                # RT predictions are absolute; match them to the occurrence of
                # this trip nearest the prediction (trip_ids repeat daily).
                if stu and not item["cancelled"]:
                    if stu["skipped"]:
                        item["cancelled"] = True
                    elif stu["time"] and abs(stu["time"] - d["sched"]) < 6 * 3600:
                        item["time"] = stu["time"]
                        item["realtime"] = True
                        # some feeds leave the delay field at 0; compute from times
                        item["delay"] = round(stu["time"] - d["sched"])
                deps.append(item)

            # Realtime-only departures: trips in the RT feed that have no
            # scheduled counterpart (e.g. BART publishes only the *next*
            # schedule period around a service change, so this week's trains
            # exist solely in realtime). Attribution to this board:
            #   - by route_id when the feed sets it, else
            #   - by trajectory fingerprint: BART leaves route_id empty and
            #     only lists stops within its prediction window, so we check
            #     whether the trip's listed downstream stops all belong to
            #     this board's route (Red trains pass SF stations that Orange
            #     never serves, and vice versa).
            board_stopset = set().union(*(st.route_stops.get(rid, set())
                                          for rid in board["route_ids"])) \
                if board["route_ids"] else set()
            for tid, u in rt_updates.items():
                if tid in sched_tids or tid in rt_cancelled:
                    continue
                stu = u["stops"].get(board["stop_id"])
                if not stu or stu["skipped"] or not stu["time"]:
                    continue
                if u["last_stop"] == board["stop_id"]:
                    continue  # terminates here; not boardable
                if u["route_id"]:
                    if u["route_id"] not in board["route_ids"]:
                        continue
                    headsign = st.route_longname.get(u["route_id"], "")
                else:
                    downstream = set(u["stops"]) - {board["stop_id"]}
                    if not downstream:
                        continue
                    frac = len(downstream & board_stopset) / len(downstream)
                    if frac < 0.8:
                        continue
                    headsign = st.stop_names.get(u["last_stop"], "")
                deps.append({
                    "sched": stu["time"],
                    "time": stu["time"],
                    "headsign": headsign,
                    "realtime": True,
                    "delay": 0,
                    "cancelled": False,
                })

            upcoming = [x for x in deps if x["time"] >= now - 30 and not x["cancelled"]]
            cancelled_soon = [x for x in deps
                              if x["cancelled"] and now - 30 <= x["sched"] <= now + 3600]
            entry["departures"] = sorted(upcoming + cancelled_soon,
                                         key=lambda x: x["time"])[:n_shown]
            entry["alerts"] = [a["text"] for a in rt_alerts
                               if not (a["routes"] or a["stops"])
                               or (board["route_ids"] & a["routes"])
                               or board["stop_id"] in a["stops"]]
            out["boards"].append(entry)
        return out

# ---------------------------------------------------------------- find-stops

def find_stops(app, text):
    ql = text.strip().lower()
    for name, src in app.sources.items():
        if not isinstance(src, Source):
            print(f"\n=== source: {name} === (no GTFS to search — "
                  f"{type(src).__name__})")
            continue
        print(f"\n=== source: {name} ===")
        try:
            if app.args.offline:
                with open(src._offline_path("gtfs.zip"), "rb") as f:
                    data = f.read()
            else:
                cache = os.path.join(app.cfg["cache_dir"], f"gtfs_{name}.zip")
                if os.path.exists(cache):
                    with open(cache, "rb") as f:
                        data = f.read()
                else:
                    print("  downloading GTFS...")
                    data = http_get(src.static_url, timeout=120)
                    with open(cache, "wb") as f:
                        f.write(data)
        except Exception as ex:
            print(f"  could not load GTFS: {ex}")
            continue

        z = zipfile.ZipFile(io.BytesIO(data))

        def rows(n):
            with z.open(n) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))

        matches = {r["stop_id"]: (r.get("stop_code", ""), r["stop_name"])
                   for r in rows("stops.txt")
                   if ql in r["stop_name"].lower()
                   or r.get("stop_code", "") == text or r["stop_id"] == text}
        if not matches:
            print("  no stops match")
            continue
        route_names = {r["route_id"]: (r.get("route_short_name") or r.get("route_long_name", ""))
                       for r in rows("routes.txt")}
        trip_route = {r["trip_id"]: r["route_id"] for r in rows("trips.txt")}
        serving = {}
        for r in rows("stop_times.txt"):
            if r["stop_id"] in matches:
                serving.setdefault(r["stop_id"], set()).add(
                    route_names.get(trip_route.get(r["trip_id"], ""), "?"))
        for sid, (code, nm) in sorted(matches.items(), key=lambda kv: kv[1][1]):
            routes = ", ".join(sorted(serving.get(sid, set()))) or "(no service)"
            print(f"  {nm}  [code {code or '-'}, id {sid}]  routes: {routes}")

# ---------------------------------------------------------------- HTTP

def make_handler(app):
    index_path = os.path.join(BASE_DIR, "index.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(index_path, "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            elif path == "/api/board":
                self._send(200, "application/json", json.dumps(app.board_json()).encode())
            else:
                self._send(404, "text/plain", b"not found")

    return Handler

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Multi-agency departure board")
    ap.add_argument("--offline", metavar="DIR",
                    help="use local feed files <source>_gtfs.zip / <source>_rt.pb from DIR")
    ap.add_argument("--now", help='freeze clock, e.g. "2026-08-04 08:00" (testing)')
    ap.add_argument("--port", type=int, help="override port from config.json")
    ap.add_argument("--find-stops", metavar="TEXT",
                    help="search stops by name/code in every source, then exit")
    args = ap.parse_args()

    cfg = load_config()
    app = App(cfg, args)

    if args.now:
        app._fake_now = datetime.strptime(args.now, "%Y-%m-%d %H:%M") \
            .replace(tzinfo=app.tz).timestamp()

    if args.find_stops:
        find_stops(app, args.find_stops)
        return

    # warn about other running instances — they share API rate limits
    try:
        r = subprocess.run(["pgrep", "-f", "shuttle_dash.py"],
                           capture_output=True, text=True)
        others = [p for p in r.stdout.split() if p != str(os.getpid())]
        if others:
            print(f"WARNING: {len(others)} other shuttle_dash.py process(es) "
                  f"running (PIDs {', '.join(others)}). They poll the same "
                  f"APIs and will trip rate limits — kill them:  "
                  f"pkill -f shuttle_dash.py  (then restart this one)")
    except Exception:
        pass

    app.start()
    if all(s.static is None for s in app.sources.values()):
        sys.exit("no GTFS static feed could be loaded; check network / URLs / token")

    port = args.port or cfg.get("port", 8146)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    print(f"Departure board running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
