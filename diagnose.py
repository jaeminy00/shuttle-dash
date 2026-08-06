#!/usr/bin/env python3
"""Diagnose why a board shows no departures.

Usage:
    python3 diagnose.py                      # all boards
    python3 diagnose.py "Red to Millbrae"    # one board

Reads the cached GTFS zips in cache_dir (run shuttle_dash.py at least once
first so they exist). Prints where departures get filtered away.
"""
import io
import os
import sys
import time
import zipfile
from datetime import datetime

from shuttle_dash import load_config, StaticGTFS
from zoneinfo import ZoneInfo

title = sys.argv[1] if len(sys.argv) > 1 else None
cfg = load_config()
tz = ZoneInfo(cfg["timezone"])
now = time.time()
today = datetime.fromtimestamp(now, tz).date()
print("now:", datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
      "| weekday:", today.strftime("%A"))

for name, scfg in cfg["sources"].items():
    boards = [b for b in cfg["boards"]
              if b["source"] == name and (not title or b["title"] == title)]
    if not boards:
        continue
    if scfg.get("type") == "bart_etd":
        print(f"\n=== source {name} === (ETD API — no GTFS; see full pipeline below)")
        continue
    cache = os.path.join(cfg["cache_dir"], f"gtfs_{name}.zip")
    print(f"\n=== source {name} ===")
    if not os.path.exists(cache):
        print("  no cached GTFS at", cache, "- run shuttle_dash.py first")
        continue
    age_h = (time.time() - os.path.getmtime(cache)) / 3600
    data = open(cache, "rb").read()
    z = zipfile.ZipFile(io.BytesIO(data))
    print(f"  cache: {len(data)} bytes, {age_h:.1f}h old | files: {sorted(z.namelist())}")

    st = StaticGTFS(data, tz, boards)
    active = st.services_on(today)
    print(f"  calendar rows: {len(st.calendar)} | calendar_dates entries: {len(st.calendar_dates)}")
    for r in st.calendar[:6]:
        print("    cal:", {k: r.get(k) for k in
              ("service_id", "monday", "tuesday", "saturday", "sunday", "start_date", "end_date")})
    print(f"  services active today: {sorted(active)[:8]}{' ...' if len(active) > 8 else ''}")

    for b in st.boards:
        print(f"\n  [{b['title']}] error: {b['error']}")
        if b.get("error") or "stop_id" not in b:
            continue
        print(f"    stop: {b['stop_id']} ({b.get('stop_resolved_name')}) | route_ids: {sorted(b['route_ids'])}")
        rows = [(tid, secs) for (tid, sid), secs in st.stop_dep.items()
                if sid == b["stop_id"] and st.trip_route[tid] in b["route_ids"]]
        print(f"    stop_times rows for these routes at this stop: {len(rows)}")

        svc = {}
        for tid, _ in rows:
            s = st.trip_service[tid]
            svc[s] = svc.get(s, 0) + 1
        for s, c in sorted(svc.items()):
            print(f"      service '{s}': {c} trips | ACTIVE TODAY: {s in active}")

        hs = {}
        for tid, _ in rows:
            h = st.trip_headsign.get(tid, "")
            hs[h] = hs.get(h, 0) + 1
        print(f"    headsigns: {hs}")
        dirf = [x.lower() for x in b.get("direction_contains", [])]
        if dirf:
            passing = sum(c for h, c in hs.items()
                          if any(f in h.lower() for f in dirf))
            print(f"    rows passing direction filter {dirf}: {passing}")

        deps = st.departures(b, now)
        print(f"    departures() -> {len(deps)}:",
              [datetime.fromtimestamp(d['sched'], tz).strftime("%a %H:%M") for d in deps[:5]])

    # ---- live realtime dump (fetches the RT feed now) ----
    rt_url = scfg.get("gtfs_rt_url", "").replace("{token}", scfg.get("token", ""))
    if not rt_url:
        print("  (no realtime URL for this source)")
        continue
    try:
        from shuttle_dash import http_get
        from google.transit import gtfs_realtime_pb2
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(http_get(rt_url, timeout=20))
        n_tu = sum(1 for e in feed.entity if e.HasField("trip_update"))
        print(f"\n  realtime: {n_tu} trip updates | feed ts: "
              f"{datetime.fromtimestamp(feed.header.timestamp, tz).strftime('%H:%M:%S')}")
        rt_routes = {}
        for e in feed.entity:
            if e.HasField("trip_update"):
                r = e.trip_update.trip.route_id or "(empty)"
                rt_routes[r] = rt_routes.get(r, 0) + 1
        print(f"  RT route_ids seen: {rt_routes}")
        for b in st.boards:
            if "stop_id" not in b:
                continue
            hits = []
            for e in feed.entity:
                if not e.HasField("trip_update"):
                    continue
                tu = e.trip_update
                for stu in tu.stop_time_update:
                    if stu.stop_id == b["stop_id"]:
                        t = (stu.departure.time if stu.HasField("departure") and stu.departure.time
                             else stu.arrival.time if stu.HasField("arrival") else 0)
                        hits.append((tu.trip.trip_id, tu.trip.route_id,
                                     datetime.fromtimestamp(t, tz).strftime("%H:%M") if t else "?"))
            print(f"  [{b['title']}] RT updates touching stop {b['stop_id']}: {len(hits)}")
            for h in hits[:6]:
                print(f"    trip {h[0]} | route {h[1]!r} | {h[2]}")
    except Exception as ex:
        print(f"  realtime fetch failed: {ex}")

# ---- full pipeline: exactly what the web server would serve right now ----
print("\n=== full pipeline (what /api/board returns) ===")
import argparse
from shuttle_dash import App

args = argparse.Namespace(offline=None, now=None, port=None, find_stops=None)
app = App(cfg, args)
for src in app.sources.values():
    src.load_static()
    src.load_realtime(time.time())
payload = app.board_json()
for b in payload["boards"]:
    if title and b["title"] != title:
        continue
    rows = [(datetime.fromtimestamp(x["time"], tz).strftime("%H:%M"),
             "live" if x["realtime"] else "sched", x["headsign"]) for x in b["departures"]]
    print(f"  [{b['title']}] error: {b['error']} | departures: {rows}")
for name, s in payload["sources"].items():
    print(f"  source {name}: rt_age={s['rt_age']} rt_error={s['rt_error']} static_error={s['static_error']}")
