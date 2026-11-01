# CLAUDE.md

Notes for AI coding assistants (and humans) continuing work on this project.
Read `Readme.md` for setup and `workout_format.md` for the file formats.

## What this is

A Python trainer app for a **Garmin Tacx NEO 2T Smart** (firmware 2.7.0) and a
**Garmin HRM 200** heart rate strap, on **Windows 11** over Bluetooth LE. It covers:
- trainer control (ERG, resistance, slope);
- live stats and per-pedal balance / pedal stroke torque;
- structured workouts, with a visual editor and a launcher.

The owner is returning to cycling after two years off, with a history of injuries.
Keep workouts short (about 30 min) and conservative, with cadence at 85–95 rpm.

## Files

| File | Role |
|---|---|
| `tacx_neo.py` | BLE core plus the CLI entry point. Contains:<br>• `TacxNeo`: connect, notifications, control over FTMS or FE-C, `TrainerState`, `SessionStats`<br>• `PedalStrokeAnalyzer`: rebuilds each crank revolution from Cycling Power Vector data<br>• `HeartRateMonitor`: BLE HR strap, auto-reconnects<br>• parsers for every characteristic<br>• all subcommands: `scan info monitor erg resistance sim ant hrm check editor launcher dashboard` |
| `tacx_workout.py` | User file and workout file loaders, plus validation that reports locations like `steps[1].steps[0]`. Also the ZWO importer, and `WorkoutPlayer`, a thread-safe state machine whose `tick()` returns trainer commands. `drive()` runs the player against a `TacxNeo`. |
| `tacx_dashboard.py` | Qt dashboard: tiles, history charts, pedal-balance "peanut", workout panel (FTP % spinner, Next button). `ReplayWorker` plays back a BTVS `.pcapng` capture instead of connecting. |
| `tacx_editor.py` | Qt visual workout editor: profile chart, step tree, property forms, live validation. |
| `tacx_launcher.py` | Qt launcher: riders and workouts with cached thumbnails; launches the dashboard and editor as subprocesses. Remembers the last rider, workout and options. |
| `tacx_ant.py` | ANT+ bicycle-power receiver for cycling dynamics, using `openant`. **Never tested with hardware.** |
| `user.json`, `users/*.json` | Rider profiles (format `tacx-user`). |
| `workouts/*.workout.json` | Workouts (format `tacx-workout`). `workouts/README.md` is the 6-week plan. |
| `.cache/` | Thumbnails (`thumbnails/<sha1>.png`) and `launcher.json` state. Not source; don't commit it. |

Dependencies are in `requirements.txt` and installed in `.venv` (Python 3.14 on the
dev machine). Run everything with `.venv\Scripts\python`.

## Threading model (GUI apps)

- **Qt owns the main thread.** Bluetooth runs in a worker `threading.Thread` with its
  own `asyncio.run()` loop (`TrainerWorker`). This also avoids bleak/WinRT trouble
  with Qt's single-threaded COM apartment on the main thread.
- **The GUI never awaits.** It polls a `Shared` object from a 100 ms `QTimer`.
- **GUI → worker:**
  - `loop.call_soon_threadsafe(...)`;
  - or `WorkoutPlayer` methods, which are protected by a lock (`set_intensity`,
    `next_step`).
- **Launcher → other apps:** it starts the dashboard and editor with
  `subprocess.Popen([sys.executable, "tacx_neo.py", ...])`.

## Protocol findings: hard-won, don't re-derive

These came from a Bluetooth HCI capture of the official Tacx app (Microsoft BTVS +
Wireshark, saved at `C:\BTP\neo.pcapng`; that file is **not in the repo**).

- **No FTMS on firmware 2.7.0.** Control goes through Tacx's FE-C-over-BLE service
  (`6e40fec1-…`, RX `…fec2` notify, TX `…fec3` write). Messages are ANT frames:
  `A4 09 4E 05 <8-byte page> <xor checksum>`.
  - Pages sent:
    - 48: resistance, 0.5% units;
    - 49: target power, 0.25 W units;
    - 50: wind;
    - 51: grade, `(grade + 200) * 100` and crr in 5e-5 units;
    - 55: user config;
    - 70: request page.
  - `--control auto` picks FTMS if it exists, otherwise FE-C. The FTMS code path has
    never run on real hardware.
  - The Tacx app also writes FE-C page `0xFC`, which is Tacx-specific and undecoded.
    It isn't needed for anything implemented here.
- **Pedal balance is NOT in Cycling Power Measurement.** The CPS Feature value is
  `0x0001002c` (wheel revs, crank revs, extreme angles, torque-based), with no
  balance bit. Measurement flags are only ever `0x0010` / `0x0030`.
- **Balance comes from the Cycling Power Vector (`0x2A64`).** Enable sequence, the same
  as the Tacx app's:
  1. enable notifications on the vector;
  2. enable indications on the CPS Control Point (`0x2A66`);
  3. write `0x0E` (Request Sampling Rate). The reply is `20 0E 01 28`, meaning
     success, 40 Hz.
- **Vector packet format:**
  - Start of each revolution: flags `0x0B`, then crank revs (u16), crank event time
    (u16, 1/1024 s), first crank angle (u16, degrees), then torque samples.
  - Other packets: flags `0x08`, then torque samples only.
  - Torque samples are s16 in 1/32 N·m, at 40 Hz. That's about 53 per revolution
    at 45 rpm.
- **Angle model:** sample `i` is at `first_angle + i * 360 / samples_in_rev`.
  Verified: the torque peaks land at about 90–110° and 260–300°, and the dead spots
  near 0° and 180°.
- **0–180° is the RIGHT leg, 180–360° the LEFT.** Confirmed by a one-legged test:
  left leg only gave L 96%. Balance = torque summed over each half
  (`PedalStrokeAnalyzer`, `right_first=True`).
- **ATT handles on this NEO:**

  | Handle | Characteristic |
  |---|---|
  | `0x1B` | CPS Measurement |
  | `0x22` | Cycling Power Vector |
  | `0x25` | CPS Control Point |
  | `0x29` | FE-C RX |
  | `0x2C` | FE-C TX |

  `ReplayWorker` relies on these handle numbers.
- **The NEO allows only one BLE connection.** It stops advertising while the Tacx app,
  Zwift or anything else is connected, so `scan` then finds nothing.
- **HRM 200:** standard Heart Rate Service (`0x180D` / `0x2A37`). It only advertises
  while worn (skin contact). Tested and working.
- **ANT+ cycling dynamics:** page layouts (`0x10 0x13 0x14 0xE0 0xE1 0xE2`, and
  capabilities subpage `0x02/0xFE`) follow the ANT+ Bicycle Power profile Rev 5.1.
  Untested.

## Testing without the trainer

- **Workout files:**
  `.venv\Scripts\python tacx_neo.py check <workout>` validates a workout and prints
  its expanded steps in watts.
- **Capture replay:**
  `.venv\Scripts\python tacx_neo.py dashboard --replay C:\BTP\neo.pcapng [--workout …]`
  feeds the real capture through the decoders.
- **Offscreen screenshots:** set `QT_QPA_PLATFORM=offscreen` and
  `QT_QPA_FONTDIR=C:/Windows/Fonts`, otherwise text renders as boxes. The dashboard,
  editor and launcher all have a hidden `--screenshot PNG` option. Fonts look
  different from on-screen rendering.
- **Player logic:** drive `WorkoutPlayer.tick(now, cadence, power)` with a fake clock.
  It returns `("erg", W) | ("grade", %) | ("resistance", %)`, or `None`.
- **Unicode output:** set `PYTHONIOENCODING=utf-8` when a test prints `✓`, `⚠` or `→`.
  The Windows console code page (cp1252) raises on them.
- **Modal dialogs hang offscreen tests.** Stub them first, e.g.
  `QtWidgets.QMessageBox.question = ...`.

## Gotchas already hit

- **pyqtgraph:** `PlotWidget` copies `clear`, `addItem`, `removeItem`, `autoRange`
  and others from its `PlotItem` onto each instance at construction. That silently
  overrides subclass methods with those names: a `Peanut.clear()` wiped the whole
  plot. Use different names (`reset()`).
- **PySide6 copies Python dicts.** Passing them through `Signal(object)` or storing
  them in `QTreeWidgetItem.setData()` gives you a QVariantMap copy, so identity
  (`is`) checks fail. Pass indices instead (`tacx_editor.py`: `self._objs`,
  `chart.spans`).
- **`QPushButton` stylesheets** with `objectName` (`#ride`) only apply after the
  app-wide stylesheet is set. Use `apply_launcher_theme` / `apply_editor_theme` in
  tests too.
- **ERG ramps** are sent at most once a second, but step changes, Next and FTP %
  changes are sent immediately (`WorkoutPlayer._command`).
- **The FTMS resistance command** uses the FTMS 1.0 UINT8 form (0.5% steps). Later
  spec versions use SINT16. Untested.
- **CLI console output is ASCII only.** Degree signs etc. garble in some consoles.

## Conventions

- Match the existing style: dataclasses, type hints, short comments explaining
  *why*, and pure parser functions kept separate from I/O.
- The CLI prints to the console. GUIs share colours and zone logic from
  `tacx_dashboard.py` (`COL_*`, `ZONES`, `zone_colour`).
- New file formats or fields need `workout_format.md` updated and validation errors
  that give the location in the file.
- Workout and user JSON is hand-edited by the owner. Keep it readable, with
  `"M:SS"` durations.

## Status

**Verified on hardware:**
- `info`
- `monitor`, including `--stroke`
- the one-legged left/right test
- HRM 200 via the `hrm` command and the dashboard

**Built and tested offscreen or by replay only:**
- the dashboard GUI
- the workout player driving real ERG
- the editor (with a mouse)
- the launcher
- ZWO import

**Untested:**
- the FTMS control path
- ANT+

**Ideas not yet done:**
- recording rides to `.fit` for Garmin Connect / Strava
- a heart-rate cap (`hr_cap`) on ERG targets
- editor undo, and drag-and-drop between repeat blocks
- an FTP test workout and FTP history in the user file
- a `.gitignore` for `.cache/` and `__pycache__/`
