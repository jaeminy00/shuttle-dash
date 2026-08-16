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

Serves two views: the tabbed dashboard at /, and the 1080x1920 portrait wall
panel at /panel (see panel.html).

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
import urllib.parse
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

# ---------------------------------------------------------------- alerts

# How loudly an alert should be shown. Most feeds leave severity_level unset,
# so the effect carries the weight in practice.
SEVERITY_RANK = {"SEVERE": 3, "WARNING": 2, "INFO": 1}
EFFECT_RANK = {"NO_SERVICE": 3, "SIGNIFICANT_DELAYS": 3,
               "DETOUR": 2, "REDUCED_SERVICE": 2, "STOP_MOVED": 2,
               "MODIFIED_SERVICE": 1, "ADDITIONAL_SERVICE": 1}


def _translated(container, lang="en"):
    """Text of a GTFS-rt TranslatedString, preferring English."""
    best = ""
    for t in container.translation:
        if not best:
            best = t.text
        if t.language.lower().startswith(lang):
            return t.text
    return best


def _enum_name(msg, field, enum_type):
    """Enum field as a name, or "" when the feed omitted it.

    GTFS-realtime is proto2 and these fields carry no explicit default, so an
    absent `effect` reads back as NO_SERVICE — the most alarming value there
    is. Only trust the value when the field is actually present.
    """
    try:
        return enum_type.Name(getattr(msg, field)) if msg.HasField(field) else ""
    except (ValueError, AttributeError):
        return ""          # field unknown to this version of the bindings


def parse_alerts(feed):
    """GTFS-realtime FeedMessage -> alert dicts (no time/route filtering)."""
    Alert = gtfs_realtime_pb2.Alert
    out = []
    for e in feed.entity:
        if not e.HasField("alert"):
            continue
        a = e.alert
        header = _translated(a.header_text).strip()
        desc = _translated(a.description_text).strip()
        if not (header or desc):
            continue
        out.append({
            "id": e.id or header[:80],
            "text": header or desc,
            "desc": desc if desc != header else "",
            "url": _translated(a.url).strip(),
            "effect": _enum_name(a, "effect", Alert.Effect),
            "severity": _enum_name(a, "severity_level", Alert.SeverityLevel),
            "periods": [(p.start or 0, p.end or 0) for p in a.active_period],
            # one selector per informed entity; the fields inside a single
            # selector are ANDed (route R *at* stop S), separate selectors ORed
            "selectors": [{"route_id": ie.route_id, "stop_id": ie.stop_id,
                           "trip_id": ie.trip.trip_id}
                          for ie in a.informed_entity],
        })
    return out


def alert_active(alert, now):
    """True when `now` falls in one of the alert's active periods.

    No active_period at all means "active until the feed drops it", which is
    how most agencies publish; an unbounded start or end is open-ended.
    """
    if not alert["periods"]:
        return True
    return any((not start or start <= now) and (not end or now <= end)
               for start, end in alert["periods"])


def alert_matches(alert, route_ids=(), stop_ids=(), trip_ids=()):
    """True when an alert informs about anything this board actually shows."""
    for s in alert["selectors"]:
        if s["route_id"] and s["route_id"] not in route_ids:
            continue
        if s["stop_id"] and s["stop_id"] not in stop_ids:
            continue
        if s["trip_id"] and s["trip_id"] not in trip_ids:
            continue
        # every field the selector pinned down matches (or it pinned down
        # nothing beyond the agency, which makes it feed-wide)
        return True
    return not alert["selectors"]


def alert_rank(alert):
    return max(SEVERITY_RANK.get(alert.get("severity", ""), 0),
               EFFECT_RANK.get(alert.get("effect", ""), 0))


def alert_public(alert):
    """The subset of an alert the front-ends need (drops feed selectors)."""
    return {k: alert[k] for k in ("id", "text", "desc", "url", "effect",
                                  "severity")} | {"rank": alert_rank(alert)}

# ---------------------------------------------------------------- realtime

class RealtimeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.updates = {}        # trip_id -> {stop_id: {"time","delay","skipped"}}
        self.cancelled = set()
        self.tu_alerts = []      # alerts riding along in the trip-update feed
        self.sa_alerts = []      # alerts from a dedicated service-alerts feed
        self.fetched_at = None
        self.error = None
        self.alerts_fetched_at = None
        self.alerts_error = None

    def alerts(self):
        """Both alert sources merged; caller must hold the lock.

        Agencies that publish a separate service-alerts endpoint often repeat
        some of it in the trip-update feed, so dedupe on id and text.
        """
        merged, seen = [], set()
        for a in self.sa_alerts + self.tu_alerts:
            key = (a["id"], a["text"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(a)
        return merged

    def ingest(self, pb_bytes, when, alerts_inline=True):
        """Ingest a trip-update feed.

        `alerts_inline` is False when the source has a dedicated alerts
        endpoint — then any alerts riding along here are redundant, and
        letting them stamp alerts_fetched_at would hide a failing alerts feed.
        """
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(pb_bytes)
        updates, cancelled = {}, set()
        for e in feed.entity:
            if not e.HasField("trip_update"):
                continue
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
        alerts = parse_alerts(feed) if alerts_inline else None
        with self.lock:
            self.updates, self.cancelled = updates, cancelled
            self.fetched_at, self.error = when, None
            if alerts is not None:
                # an empty list is a real answer: the feed says "nothing wrong"
                self.tu_alerts = alerts
                self.alerts_fetched_at, self.alerts_error = when, None

    def ingest_alerts(self, pb_bytes, when):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(pb_bytes)
        alerts = parse_alerts(feed)
        with self.lock:
            self.sa_alerts = alerts
            self.alerts_fetched_at, self.alerts_error = when, None

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
        # Some agencies bundle alerts into the trip-update feed (TripShot);
        # others publish them at their own endpoint (AC Transit, 511).
        self.alerts_url = scfg.get("gtfs_alerts_url", "").replace("{token}", token)
        self.boards_cfg = [b for b in cfg["boards"] if b["source"] == name]
        self.poll_seconds = scfg.get("poll_seconds", cfg.get("rt_poll_seconds", 20))
        self.alerts_poll_seconds = scfg.get(
            "alerts_poll_seconds", cfg.get("alerts_poll_seconds", 120))
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
            self.rt.ingest(data, now, alerts_inline=not self.alerts_url)
        except Exception as ex:
            with self.rt.lock:
                self.rt.error = str(ex)
            print(f"[rt:{self.name}] fetch failed: {ex}", file=sys.stderr)

    def load_alerts(self, now):
        if not self.alerts_url:
            return
        try:
            if self.args.offline:
                path = self._offline_path("alerts.pb")
                if not os.path.exists(path):
                    return          # optional in offline fixtures
                with open(path, "rb") as f:
                    data = f.read()
            else:
                data = http_get(self.alerts_url, timeout=20)
            self.rt.ingest_alerts(data, now)
            with self.rt.lock:
                n = len(self.rt.sa_alerts)
            print(f"[alerts:{self.name}] {n} alert(s) in feed")
        except Exception as ex:
            # a broken alerts endpoint must not mark the departures stale
            with self.rt.lock:
                self.rt.alerts_error = str(ex)
            print(f"[alerts:{self.name}] fetch failed: {ex}", file=sys.stderr)


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
        self.alerts_poll_seconds = 0      # no alert feed either
        self.rt = RealtimeState()
        self.static = True
        self.static_error = None
        self.static_loaded_at = time.time()
        self.lock = threading.Lock()

    def load_static(self):
        print(f"[static:{self.name}] fixed timetable source (no feed)")

    def load_realtime(self, now):
        pass

    def load_alerts(self, now):
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
    # BART publishes service advisories on its own endpoint, not in the ETD
    # payload; they are system-wide, so every board on this source shows them.
    DEFAULT_BSA_URL = "https://api.bart.gov/api/bsa.aspx?cmd=bsa&key={token}&json=y"

    def __init__(self, name, scfg, cfg, args, tz):
        self.name = name
        self.cfg = cfg
        self.args = args
        self.tz = tz
        url = scfg.get("etd_url", self.DEFAULT_URL)
        bsa = scfg.get("bsa_url", self.DEFAULT_BSA_URL)
        token = scfg.get("token", "")
        if not token:  # empty token -> drop the key parameter entirely
            url = url.replace("key={token}&", "").replace("&key={token}", "")
            bsa = bsa.replace("key={token}&", "").replace("&key={token}", "")
        self.url = (url.replace("{token}", token)
                       .replace("{station}", scfg.get("station", "ALL")))
        self.bsa_url = bsa.replace("{token}", token)
        self.station_name = scfg.get("station", "")
        self.poll_seconds = scfg.get("poll_seconds", cfg.get("rt_poll_seconds", 20))
        self.alerts_poll_seconds = scfg.get(
            "alerts_poll_seconds", cfg.get("alerts_poll_seconds", 120))
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

    def load_alerts(self, now):
        """BART service advisories (bsa.aspx), shaped like GTFS-rt alerts."""
        if not self.bsa_url:
            return
        try:
            if self.args.offline:
                path = os.path.join(self.args.offline, f"{self.name}_bsa.json")
                if not os.path.exists(path):
                    return
                with open(path) as f:
                    doc = json.load(f)
            else:
                doc = json.loads(http_get(self.bsa_url, timeout=20).decode("utf-8-sig"))
            raw = (doc.get("root") or {}).get("bsa") or []
            if isinstance(raw, dict):        # single advisory comes unwrapped
                raw = [raw]
            alerts = []
            for i, b in enumerate(raw):
                cdata = b.get("description") or {}
                text = (cdata.get("#cdata-section") if isinstance(cdata, dict)
                        else cdata) or ""
                text = text.strip()
                # the "all clear" placeholder is not an alert
                if not text or "no delays reported" in text.lower():
                    continue
                kind = str(b.get("type", "")).upper()
                alerts.append({
                    "id": f"bsa-{b.get('@id', i)}",
                    "text": text,
                    "desc": "",
                    "url": "",
                    "effect": "SIGNIFICANT_DELAYS" if kind == "DELAY" else "",
                    "severity": "SEVERE" if kind == "EMERGENCY" else "WARNING",
                    "periods": [],       # BART drops advisories when they end
                    "selectors": [],     # system-wide
                })
            with self.rt.lock:
                self.rt.sa_alerts = alerts
                self.rt.alerts_fetched_at, self.rt.alerts_error = now, None
            print(f"[alerts:{self.name}] {len(alerts)} advisory(ies)")
        except Exception as ex:
            with self.rt.lock:
                self.rt.alerts_error = str(ex)
            print(f"[alerts:{self.name}] fetch failed: {ex}", file=sys.stderr)

# ---------------------------------------------------------------- app

class WeatherSource:
    """Current conditions + hourly outlook from Open-Meteo (no API key).

    Open-Meteo is keyless and its forecast only moves hourly, so this polls
    on its own slow cadence in its own thread. Every failure is non-fatal:
    the panel hides the weather rows and keeps showing departures.
    """

    URL = ("https://api.open-meteo.com/v1/forecast"
           "?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,weather_code"
           "&hourly=temperature_2m,precipitation_probability"
           "&daily=temperature_2m_max,temperature_2m_min,sunset"
           "&temperature_unit=fahrenheit&timeformat=unixtime"
           "&timezone={tz}&forecast_days=2")

    # WMO weather interpretation codes -> short label
    CODES = {
        0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        56: "Freezing drizzle", 57: "Freezing drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        66: "Freezing rain", 67: "Freezing rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
        80: "Rain showers", 81: "Rain showers", 82: "Heavy showers",
        85: "Snow showers", 86: "Snow showers",
        95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
    }

    def __init__(self, wcfg, cfg, args, tz):
        self.wcfg = wcfg
        self.args = args
        self.tzname = cfg["timezone"]
        self.poll_seconds = wcfg.get("poll_seconds", 900)
        self.hours = wcfg.get("hours_shown", 12)
        self.lock = threading.Lock()
        self.raw = None
        self.error = None
        self.fetched_at = None

    def load(self):
        try:
            if self.args.offline:
                with open(os.path.join(self.args.offline, "weather.json")) as f:
                    raw = json.load(f)
            else:
                url = self.URL.format(lat=self.wcfg["lat"], lon=self.wcfg["lon"],
                                      tz=urllib.parse.quote(self.tzname))
                raw = json.loads(http_get(url, timeout=20))
            with self.lock:
                self.raw, self.error, self.fetched_at = raw, None, time.time()
        except Exception as ex:
            with self.lock:
                self.error = str(ex)[:200]
            print(f"[weather] fetch failed: {ex}")

    def snapshot(self, now):
        with self.lock:
            raw, err, fetched = self.raw, self.error, self.fetched_at
        if raw is None:
            return {"error": err or "no weather data yet", "age": None,
                    "hourly": []}
        try:
            cur = raw.get("current") or {}
            hourly = raw.get("hourly") or {}
            daily = raw.get("daily") or {}
            times = hourly.get("time") or []
            temps = hourly.get("temperature_2m") or []
            pops = hourly.get("precipitation_probability") or []

            # the outlook starts at the first hour ahead of now
            start = next((i for i, t in enumerate(times) if t > now), len(times))
            ahead = []
            for i in range(start, min(start + self.hours, len(times))):
                t = temps[i] if i < len(temps) else None
                if t is None:
                    continue
                p = pops[i] if i < len(pops) else None
                ahead.append({"time": times[i],
                              "temp_f": round(t),
                              "temp_c": round((t - 32) * 5 / 9),
                              "pop": round(p) if p is not None else 0})

            # hi/lo and sunset come from the daily row covering `now`
            dts = daily.get("time") or []
            di = max((i for i, t in enumerate(dts) if t <= now), default=0)

            def dval(key):
                v = daily.get(key) or []
                return v[di] if di < len(v) else None

            hi, lo, sunset = (dval("temperature_2m_max"),
                              dval("temperature_2m_min"), dval("sunset"))
            temp = cur.get("temperature_2m")
            return {
                "temp_f": round(temp) if temp is not None else None,
                "temp_c": round((temp - 32) * 5 / 9) if temp is not None else None,
                "cond": self.CODES.get(cur.get("weather_code"), ""),
                "hi_f": round(hi) if hi is not None else None,
                "lo_f": round(lo) if lo is not None else None,
                "sunset": sunset,
                "rain_pct": ahead[0]["pop"] if ahead else 0,
                "hourly": ahead,
                "age": round(now - fetched) if fetched else None,
                "error": err,
            }
        except Exception as ex:
            return {"error": f"malformed weather payload: {ex}", "age": None,
                    "hourly": []}

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

        wcfg = cfg.get("weather") or {}
        self.weather = (WeatherSource(wcfg, cfg, args, self.tz)
                        if wcfg.get("lat") is not None
                        and wcfg.get("lon") is not None else None)

    def now(self):
        if self.args.now:
            return self._fake_now
        return time.time()

    def start(self):
        """Bring the feeds up.

        Offline mode loads synchronously: the files are local, and the testing
        paths want a fully populated app before the first request.

        Otherwise every source loads on its own thread and this returns at
        once, so main() can bind the socket before any feed is fetched. A
        kiosk browser pointed here at boot then gets a page — boards reading
        "loading schedule…" until their source lands — instead of a connection
        refusal, which a kiosk browser has no way to retry. It also means one
        slow agency delays only its own cards rather than the whole board:
        AC Transit's schedule alone takes 15-30 s to parse on a Pi.
        """
        if self.args.offline:
            for src in self.sources.values():
                src.load_static()
                src.load_realtime(self.now())
                src.load_alerts(self.now())
            if self.weather:
                self.weather.load()
            return

        for src in self.sources.values():
            threading.Thread(target=self._poll_source, args=(src,),
                             daemon=True).start()
            if src.alerts_poll_seconds:
                threading.Thread(target=self._poll_alerts, args=(src,),
                                 daemon=True).start()
        threading.Thread(target=self._static_refresher, daemon=True).start()
        if self.weather:
            threading.Thread(target=self._poll_weather, daemon=True).start()

    def _poll_source(self, src):
        # the schedule parse happens here rather than before the socket binds;
        # until it finishes this source's boards report "not loaded yet"
        src.load_static()
        # each source has its own cadence (rate-limited APIs poll slower)
        while True:
            src.load_realtime(self.now())
            time.sleep(src.poll_seconds)

    def _poll_alerts(self, src):
        # alerts change on the order of hours; polling them at the departure
        # cadence would burn the same rate limit for no new information
        while True:
            src.load_alerts(self.now())
            time.sleep(src.alerts_poll_seconds)

    def _poll_weather(self):
        while True:
            self.weather.load()
            time.sleep(self.weather.poll_seconds)

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
               "sources": {}, "default_tab": self.cfg.get("default_tab", "work"),
               # consumed by the portrait wall panel (/panel); harmless to the
               # tabbed dashboard, which ignores them
               "weather": self.weather.snapshot(now) if self.weather else None,
               "house": self.cfg.get("house", {}),
               "panel": self.cfg.get("panel", {})}
        n_shown = self.cfg.get("departures_shown", 3)

        # active alerts per source, resolved once and shared by its boards
        src_alerts = {}
        for name, src in self.sources.items():
            with src.rt.lock:
                out["sources"][name] = {
                    "rt_age": (round(now - src.rt.fetched_at)
                               if src.rt.fetched_at else None),
                    "rt_error": src.rt.error,
                    "static_error": src.static_error,
                    "poll_seconds": src.poll_seconds,
                    "alerts_age": (round(now - src.rt.alerts_fetched_at)
                                   if src.rt.alerts_fetched_at else None),
                    "alerts_error": src.rt.alerts_error,
                }
                src_alerts[name] = [a for a in src.rt.alerts()
                                    if alert_active(a, now)]

        # alerts that reach at least one board, deduped across boards:
        # key -> {"alert": public dict, "boards": [titles]}
        shown_alerts = {}

        def attach(entry, source, matched):
            entry["alerts"] = [alert_public(a) for a in matched]
            for a in matched:
                slot = shown_alerts.setdefault(
                    (source, a["id"], a["text"]),
                    {"alert": alert_public(a) | {"source": source}, "boards": []})
                slot["boards"].append(entry["title"])

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
                # BART advisories are system-wide — every ETD board gets them
                attach(entry, bcfg["source"], src_alerts[bcfg["source"]])
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

            deps = []
            sched_tids = set()
            shown_tids = set()      # every trip this board can put on screen
            for d in st.departures(board, now):
                sched_tids.add(d["trip_id"])
                shown_tids.add(d["trip_id"])
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
                shown_tids.add(tid)
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
            attach(entry, bcfg["source"],
                   [a for a in src_alerts[bcfg["source"]]
                    if alert_matches(a, route_ids=board["route_ids"],
                                     stop_ids={board["stop_id"]},
                                     trip_ids=shown_tids)])
            out["boards"].append(entry)

        # loudest first, so a single-line display (the wall panel) leads with
        # the alert that actually changes what you do
        out["alerts"] = sorted(
            ({**slot["alert"], "boards": slot["boards"]}
             for slot in shown_alerts.values()),
            key=lambda a: (-a["rank"], a["text"]))
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
    panel_path = os.path.join(BASE_DIR, "panel.html")

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
            elif path in ("/panel", "/panel.html"):
                with open(panel_path, "rb") as f:
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

    # Bind before the feeds are fetched. Constructing the server listens
    # immediately, so a kiosk browser that starts alongside us connects and
    # gets a board rather than a refusal it will never retry — the cards fill
    # in as each source finishes loading.
    port = args.port or cfg.get("port", 8146)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))

    app.start()
    if args.offline and all(s.static is None for s in app.sources.values()):
        sys.exit(f"no GTFS static feed loaded from {args.offline}; "
                 f"expected <source>_gtfs.zip there")

    print(f"Departure board running at http://localhost:{port}")
    print(f"Wall panel (1080x1920 portrait) at http://localhost:{port}/panel")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
