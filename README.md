# Departure Board

A glanceable departure board for a living-room screen, with two togglable
tabs:

- **WORK** — LBNL shuttle (Blue Uphill), AC Transit 52 toward campus,
  F at Hearst & Walnut, and the Bear Transit P Line, all near Oxford &
  University.
- **SF** — F Transbay to Salesforce TC (Shattuck & Kittredge) plus BART
  from Downtown Berkeley: Red line toward Millbrae/SFO and Orange line
  toward Berryessa/North San José.

Live GTFS-realtime predictions, countdowns, and a walk-time-aware
"leave now" cue per stop. The tab choice is remembered per browser; a
kiosk can pin one with `http://localhost:8146/?tab=work` or `?tab=sf`
(`default_tab` in config sets the fallback).

## Files

| file | purpose |
|---|---|
| `shuttle_dash.py` | the whole backend: fetches feeds, merges schedule + realtime, serves the page |
| `index.html` | the dashboard UI (one card per board) |
| `config.json` | feed sources, boards, walk times, port |

Keep the three files in one folder.

## Setup

```bash
# in your venv (uv):
uv pip install gtfs-realtime-bindings

# 1. Copy the example config (config.json is gitignored — your tokens stay local):
cp config.example.json config.json
# 2. Get free API tokens (all instant, no approval wait):
#      AC Transit: https://api.actransit.org  (register / sign up)
#      BART:       https://api.bart.gov (register)
#      511.org:    https://511.org/open-data/token (Muni & other Bay Area GTFS)
# 3. Paste them into config.json where it says PASTE_YOUR_..._HERE
python3 shuttle_dash.py
```

Then open **http://localhost:8146**.

BART uses its own real-time **ETD API** (`type: "bart_etd"` in config) —
the same per-station departures third-party apps use. Each estimate
carries destination, minutes, direction, line color, and delay directly,
so it needs no schedule and is immune to GTFS schedule-change gaps. It
needs its own free key (https://api.bart.gov — register); boards on this
source filter with `color` ("RED"/"ORANGE") and `direction`
("South"/"North"), and `station` in the source is the BART abbreviation
(DBRK = Downtown Berkeley).
**Bear Transit (P/C Line):** no public feed exists — the abandoned Trillium
GTFS ended in January 2022, Bear Transit is not a 511.org operator, and its
live tracking flows privately into the Transit app. The P/C cards therefore
use a `type: "timetable"` source: departure times copied from the official
printed timetables (pt.berkeley.edu, Oct 2025) live directly in each
board's `times` list in config.json (`days: "weekdays"`). Update those
lists when campus publishes new timetables; campus holidays are not
modeled.

On startup the app resolves each board's route and stop against the real
feeds and prints what it matched, e.g.:

```
[52 to campus] stop 'Hearst Av' -> Hearst Av & Oxford St (id ..., code ...)
```

If a stop is ambiguous or wrong, the card shows the candidates and you can
search the feeds yourself:

```bash
python3 shuttle_dash.py --find-stops "hearst"
```

…which lists every matching stop in every source with its stop code and
the routes that actually serve it. Put the right stop **code** in
`config.json`.

The AC Transit stops are pinned to exact codes (50400, 52848, 55999). The
P Line and BART boards ship with name fragments ("Oxford", "Downtown
Berkeley") and route fragments ("Millbrae", "Berryessa") because their
feeds' exact naming couldn't be verified offline — check the startup log
lines the first time you run it, and if a card reports ambiguity or a
wrong match, `--find-stops` gives you the codes to pin.

## Reading the board

- **Big number** — minutes until the next bus *you can still catch*, given
  that board's walk time. Buses that leave sooner than you can walk there
  are struck through ("too late").
- **Status pill** — `✓ Leave by 11:32` (margin ≥ 5 min), `⏰ Leave soon`
  (< 5 min), `🏃 Leave now` (< 2 min, pulses).
- **→ headsign** — shown automatically when a stop serves more than one
  destination (the F stops there in both directions).
- **`● live` vs `sched`** — realtime prediction vs printed schedule;
  `+3 min` is lateness vs schedule. Cancelled trips show struck-through in red.
- **walk − / +** on each card adjusts that stop's walk time (saved in the
  browser; defaults come from `config.json`).
- If a realtime feed stops responding, its cards fall back to the schedule
  and show a stale warning — the board never goes blank.

## Config

- `sources` — each has a GTFS static URL and a GTFS-realtime (trip updates)
  URL (empty string = schedule-only). `{token}` in a URL is replaced with
  the source's `token` value.
- `boards` — one card each: `tab` ("work"/"sf"/anything — tabs are built
  from whatever appears here), `source`, `route` (exact short/long route
  name, falling back to a substring so one fragment can catch both
  direction-variants of a BART line), `stop` (stop code, stop id, exact
  name, or unique name fragment — or a **list** of those, tried in order
  until one matches, e.g. `["904202", "Downtown Berkeley"]`),
  `walk_minutes`, and optional
  `direction_contains` — a list of headsign substrings that filters
  departures to one direction (used for BART southbound; it also steers
  stop resolution to the right platform).
- `departures_shown` — rows per card (3).
- `rt_poll_seconds` / `static_refresh_hours` — polling cadence. Schedules
  are cached in `~/.cache/shuttle-dash` and refreshed daily.
- `port` — HTTP port (8146).

Add or remove boards freely — cards lay themselves out in a grid.

## Testing without the network

```bash
python3 shuttle_dash.py --offline DIR      # DIR holds <source>_gtfs.zip + <source>_rt.pb
python3 shuttle_dash.py --now "2026-08-04 08:00"
python3 shuttle_dash.py --port 9000
```

## Raspberry Pi notes

The layout auto-compacts below ~500 px of height (one row per card,
tested at 480×320). Rough kiosk recipe:

```bash
sudo apt install chromium-browser
pip install gtfs-realtime-bindings
```

Autostart the server with systemd — `/etc/systemd/system/shuttle-dash.service`:

```ini
[Unit]
Description=Departure board
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/shuttle-dash/shuttle_dash.py
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
```

Then launch the browser in kiosk mode:

```
chromium-browser --kiosk --noerrdialogs --disable-session-crashed-bubble http://localhost:8146
```

Disable screen blanking (`raspi-config` → Display → Screen Blanking → off).
The server binds `0.0.0.0`, so you can also open `http://<pi-address>:8146`
from any device on your network.

Note: AC Transit's full GTFS is much bigger than the LBNL one — first
parse may take ~15–30 s on an older Pi. It happens once a day in a
background thread; the board keeps serving the old schedule meanwhile.

## How it works (for future tinkering)

- **Schedule**: each source's GTFS zip is streamed and filtered to just
  the configured routes+stops; service days come from `calendar.txt` +
  `calendar_dates.txt`; times are computed DST-safely in
  `America/Los_Angeles` (GTFS "25:00"-style times are handled).
- **Realtime**: GTFS-RT TripUpdates are matched by `trip_id` (TripShot's
  feed carries no `route_id`, so the join goes through the static feed).
  Predictions are absolute times, matched to the day's scheduled run
  (±6 h); delay is computed from the time difference because some feeds
  leave the `delay` field at 0. Cancelled trips and skipped stops are
  handled. Alerts, if a feed carries them, show per-card.
- **Stop resolution**: `stop` in config accepts stop_id → stop_code →
  exact name → name fragment; a fragment must be unique *among stops the
  route actually serves*, otherwise the card lists the candidates.
