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
| `shuttle_dash.py` | the whole backend: fetches feeds, merges schedule + realtime, serves the pages |
| `index.html` | the dashboard UI (one card per board), served at `/` |
| `panel.html` | the 1080 × 1920 portrait wall panel, served at `/panel` |
| `config.json` | feed sources, boards, walk times, weather, panel groups, port |

Keep the files in one folder. `index_design.html` and `panel_design.html` are
copies used for design previews — they fall back to demo data when no server
is reachable. `panel_design.html` differs from `panel.html` by exactly one
line, so regenerate it after editing the panel:

```bash
sed 's/^const ALLOW_DEMO = false;$/const ALLOW_DEMO = true;/' panel.html > panel_design.html
```

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

Then open **http://localhost:8146** for the tabbed dashboard, or
**http://localhost:8146/panel** for the portrait wall panel.

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

## Wall panel (`/panel`)

A second view for a portrait screen on the wall — authored at exactly
1080 × 1920 and scaled to fit whatever it's opened on, so it letterboxes
rather than reflowing. It answers one question from across the room: *how
long until I have to walk out the door?*

- **Groups** — one block per travel goal, not per bus. Each block's big
  number is the soonest departure across *all* its routes that you can
  still walk to, minus that board's walk time; the rows beneath list the
  next few arrival times per route. Times you can no longer reach are
  dimmed (same idea as the dashboard's "too late" strikethrough).
- **Header** — clock, date, and current conditions in °F / °C with today's
  high and low.
- **Next 12 hours** — temperature in both units and rain chance per hour,
  bar height proportional to probability.
- **Footer** — bin day, and a standing note you set in config.
- **Night state** — between `night_start` and `night_end` the panel drops
  to a dim clock, the date, the first bus, and the temperature. Set
  `"night_start": null` to disable it.
- **Degradation** — if the weather fetch fails the header and outlook
  hide themselves and departures take the space; if `/api/board` stops
  responding for two minutes the NOTE slot turns into a stale warning so
  a frozen clock can't be mistaken for live times.

Kiosk it the same way as the dashboard, pointed at `/panel`:

```
chromium-browser --kiosk --noerrdialogs --disable-session-crashed-bubble http://localhost:8146/panel
```

## Config

- `weather` — `lat` / `lon` for [Open-Meteo](https://open-meteo.com)
  (no API key, no signup). `poll_seconds` (900) and `hours_shown` (12)
  are optional. **Delete this block to drop weather from the panel** —
  everything else keeps working.
- `house` — `trash_day` (a weekday name), `trash_kinds`, and `note`, the
  standing line at the bottom of the panel. Omit any of them to hide that
  row.
- `panel` — `groups` is a list of `{title, accent, boards}`, where each
  entry in `boards` is `{board, label, via}`: `board` matches a board's
  `title`, `label` is the short route badge ("BLUE", "52"), and `via`
  names the route in the group's subtitle. Also `night_start` /
  `night_end` ("22:00" / "06:00") and `times_shown` (3). Without a
  `panel` block the panel falls back to one group per board on the
  default tab.
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

In `--offline` mode the weather comes from `DIR/weather.json` (a saved
Open-Meteo response) if that file exists, and is skipped otherwise. To
check the panel's layout with no backend at all, open `panel_design.html`
straight from disk — it renders against built-in demo data.

## Raspberry Pi notes

The layout auto-compacts below ~500 px of height (one row per card,
tested at 480×320). Rough kiosk recipe:

```bash
sudo apt install cog          # WPE WebKit kiosk browser — no X, no desktop
pip install gtfs-realtime-bindings
```

**Browser choice matters more than the Pi does.** Cog renders straight to
DRM/KMS with no X server and no window manager, which on a 1 GB Pi 3B+ is
the difference between fitting in RAM and swapping to the SD card —
figure ~150–220 MB against Chromium-plus-a-desktop's 400–700 MB.
Chromium still works if you prefer it; give it `--kiosk --noerrdialogs
--disable-session-crashed-bubble` and expect to want a Pi 4.

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

Then launch the browser:

```
cog http://localhost:8146/panel
```

Disable screen blanking (`raspi-config` → Display → Screen Blanking → off).
The server binds `0.0.0.0`, so you can also open `http://<pi-address>:8146`
from any device on your network.

### A portrait panel on a landscape screen

`/panel` is authored portrait (1080 × 1920). If the physical screen is
landscape, add `?rotate=90` (or `270`) to the kiosk URL:

```
cog 'http://localhost:8146/panel?rotate=90'
```

The page already applies one composited transform to scale itself, so
folding the rotation into that same matrix is free. On a 1920 × 1080
screen this lands at **scale 1.0 — a pixel-exact fit** at the authored
resolution. Prefer this over rotating the display: VC4 has no general 90°
hardware rotate, so `video=HDMI-A-1:1920x1080@60,rotate=90` on the kernel
command line can silently fall back to rotating every frame on the CPU.
Rotate at the display level only if you also need the console and the `/`
dashboard rotated, and check for stutter if you do.

### Startup

The server binds its socket *before* fetching any feed, and each source
loads on its own thread — so a kiosk browser starting alongside it always
gets a page. Boards whose schedule hasn't arrived yet read "loading
schedule…" and fill in as their source lands; measured cold start to first
`/api/board` response is ~0.1 s, with all boards populated within ~10 s.
This matters most with Cog, which has no user to press reload and does not
retry a failed load on its own.

AC Transit's full GTFS is much bigger than the LBNL one — first parse may
take ~15–30 s on an older Pi (639k stop-times). That no longer delays
startup, and the daily refresh happens in a background thread with the old
schedule still being served.

`--offline` still loads synchronously, so test runs have a fully populated
app before the first request.

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
- **Rendering**: both pages tick once a second, but the DOM writes are
  idempotent — `setText` / `setHTML` / `setStyle` build the value, compare it
  to what was last written, and only touch the document when it differs. A
  second in which nothing visibly changed therefore costs no layout or paint,
  which is what keeps a low-power kiosk smooth. Measured over 60 ticks with
  unchanged data, `/panel` does **one** `innerHTML` rebuild (the minute
  rollover) rather than 120. Keep new per-tick output flowing through those
  helpers; assigning `innerHTML` directly in a render path puts the cost back.
  The one genuinely per-second value on `/` is each card's "live 12s ago"
  counter, which is deliberately written by `tickFreshness()` into spans the
  card markup leaves empty, so it can update without rebuilding the card.
  Intl formatters are cached in `dtf()` — constructing them per call was a
  large share of the tick's CPU.
