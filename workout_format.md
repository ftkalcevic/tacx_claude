# Workout file format

Two kinds of JSON file are involved:
- a **user file** (`user.json`) describing the rider: name, weight, FTP and so on;
- **workout files** (`*.workout.json`) describing what to ride.

Workouts never contain rider-specific numbers. They give power as a % of FTP, and the
player takes the FTP from the user file. The same workout file therefore works for any
rider, and keeps working as your FTP changes.

## User file

```json
{
  "format": "tacx-user",
  "version": 1,
  "name": "Frank Tkalcevic",
  "nickname": "Frank",
  "dob": "1970-01-31",
  "sex": "male",
  "weight_kg": 80.0,
  "height_cm": 180,
  "ftp": 250,
  "ftp_tested": "2026-10-15",
  "max_hr": 180,
  "resting_hr": 52,
  "bike_weight_kg": 8.5
}
```

| Key | Type | Required | Meaning |
|---|---|---|---|
| `$schema` | string | no | Path to `schemas/tacx-user.schema.json`, for editor IntelliSense. Ignored by the program. |
| `format` | string | yes | Always `"tacx-user"`. |
| `version` | integer | yes | Format version, currently `1`. |
| `name` | string | yes | Full name. |
| `nickname` | string | no | Short name shown on the dashboard; defaults to `name`. |
| `dob` | `"YYYY-MM-DD"` | no | Date of birth. Used to work out age, e.g. to estimate max HR when `max_hr` isn't given. |
| `sex` | string | no | `"male"`, `"female"` or omitted. Reserved for future calorie and zone estimates. |
| `weight_kg` | number | yes | Rider weight. Sent to the trainer (FE-C page 55) for slope-mode physics, and used for W/kg. |
| `height_cm` | number | no | Reserved for future use. |
| `ftp` | number (W) | yes | Functional threshold power. Converts every `ftp_pct` target into watts. |
| `ftp_tested` | `"YYYY-MM-DD"` | no | When the FTP was last tested. The dashboard can show it as a reminder. |
| `max_hr` | number (bpm) | no | Maximum heart rate, for heart-rate zones. |
| `resting_hr` | number (bpm) | no | Resting heart rate, for heart-rate-reserve zones. |
| `bike_weight_kg` | number | no | Bike weight sent to the trainer; default 8 kg. |

Where the player finds the user file:
1. the path given with `--user PATH`;
2. otherwise `user.json` in the current folder;
3. otherwise `user.json` next to `tacx_neo.py`.

Command-line values override the file for one session only, without changing it:
`--ftp`, `--weight` and `--bike-weight`.

The file holds personal details such as date of birth. It is only read locally and is
never sent anywhere, apart from weight going to the trainer.

## Workout files

A structured workout is a JSON file (`*.workout.json`). A workout is
an ordered list of **steps**. Each step holds one target (power, slope, resistance or
free ride) for a set duration. Steps can be grouped into **repeat** blocks, and blocks
can be nested.

Why JSON: it's easy to hand-edit, Python reads it with no extra libraries, and nested
repeats are natural. Existing Zwift `.zwo` (XML) workouts can also be imported; see the
last section.

## Example

```json
{
  "format": "tacx-workout",
  "version": 1,
  "name": "VO2 5x3",
  "description": "Five 3-minute VO2 max efforts with 2-minute recoveries.",
  "author": "Frank",
  "steps": [
    { "name": "Warm up", "duration": "10:00", "ftp_pct": [50, 75], "cadence": [85, 95],
      "text": [ { "at": "0:00", "msg": "Easy spin to start" },
                { "at": "9:30", "msg": "Intervals in 30 s" } ] },

    { "repeat": 5, "steps": [
      { "name": "On",  "duration": "3:00", "ftp_pct": 110, "cadence": [95, 105] },
      { "name": "Off", "duration": "2:00", "ftp_pct": 55 }
    ] },

    { "name": "Hill", "duration": "4:00", "grade": 4.0 },
    { "name": "Spin out", "duration": 120, "free": true },
    { "name": "Cool down", "duration": "8:00", "ftp_pct": [65, 45] }
  ]
}
```

## Top level

| Key | Type | Required | Meaning |
|---|---|---|---|
| `$schema` | string | no | Path to `schemas/tacx-workout.schema.json`, for editor IntelliSense. Ignored by the program. |
| `format` | string | yes | Always `"tacx-workout"`. |
| `version` | integer | yes | Format version, currently `1`. |
| `name` | string | yes | Shown on the dashboard. |
| `description` | string | no | Free text. |
| `author` | string | no | Free text. |
| `steps` | array | yes | Steps and repeat blocks, run in order. |

## Steps

Each step has a `duration` and **exactly one** target key.

| Key | Type | Meaning |
|---|---|---|
| `name` | string | Optional label shown on the dashboard, e.g. "On" or "Recovery". |
| `duration` | integer seconds, or `"M:SS"` / `"H:MM:SS"` string | How long the step lasts. Required. |
| `ftp_pct` | number, or `[start, end]` | **ERG mode** power as a % of the rider's FTP (from the user file). A pair is a linear ramp from start to end. This is the usual way to set power. |
| `watts` | number, or `[start, end]` | **ERG mode** power in absolute watts, the same for every rider. A pair is a ramp. |
| `grade` | number (%) | **Slope mode** gradient, -10 to 20. The trainer adjusts resistance to your speed, like a real road. |
| `resistance` | number (%) | **Resistance mode**, 0–100% of the trainer's brake. |
| `free` | `true` | Free ride: flat road, 0% slope, with no target. |
| `cadence` | number, or `[low, high]` (rpm) | Optional cadence target; a single number means ±5 rpm. The dashboard shows it and warns when you're outside it. The trainer isn't controlled by it. |
| `text` | array of `{ "at": duration, "msg": string }` | Optional on-screen messages. `at` is measured from the start of the step. |

### Repeat blocks

```json
{ "repeat": 4, "name": "Over-unders", "steps": [ ... ] }
```

- `repeat`: how many times to run the block (integer, 1 or more).
- `steps`: the steps or blocks inside it. Blocks can be nested, e.g. 3 sets of 4 × 30/30.
- `name`: optional. The dashboard shows a counter such as "Over-unders 2/4".

## How it drives the trainer

| Target | Trainer command | Notes |
|---|---|---|
| `ftp_pct`, `watts` | ERG: `set_target_power` | Ramps are re-sent every second, rounded to 1 W. The NEO settles on a new power within about 1–2 s. |
| `grade` | Slope: `set_simulation(grade)` | Uses `weight_kg` and `bike_weight_kg` from the user file, sent to the trainer when the workout starts. |
| `resistance` | `set_resistance` | |
| `free` | `set_simulation(0)` | |

How the player behaves:
- **Starting:** the workout clock starts when you start pedalling. The first target is
  sent straight away.
- **Step changes** happen exactly on the boundary, and the next target is sent right
  away.
- **Pausing:** if cadence stays at 0 for 3 s, the workout clock pauses, and it resumes
  when you start pedalling again. This is on by default and turned off with
  `--no-autopause`.
- **Next button:** skips to the start of the next step.
- **FTP spinner:** sets FTP as a percentage, 50–150% in 1% steps, default 100%. All
  `ftp_pct` targets change at once. The base FTP from the user file and the adjusted
  FTP are shown beside it. `watts` steps aren't affected.
- **Finishing:** at the end of the workout the trainer is set to a flat road (0% slope).

## Running a workout

Check a workout and list its expanded steps, with watts for your FTP:
```
python tacx_neo.py check workouts\vo2_5x3.workout.json
```

Edit it visually: the chart is drawn as on the dashboard, with a step tree and property forms below. Leave out the file to start a new workout:
```
python tacx_neo.py editor workouts\vo2_5x3.workout.json
```

Ride it on the dashboard:
```
python tacx_neo.py dashboard --workout workouts\vo2_5x3.workout.json [--hrm] [--ftp 260] [--user other.json]
```

## Validation

The loader checks a workout before it starts and reports the location of any problem,
for example `steps[1].steps[0]`:
- unknown keys, a missing `duration`, or not exactly one target key;
- negative or zero durations, and `repeat` below 1;
- in the user file: missing required keys, an unreadable date, or values outside sensible
  ranges (weight 30–200 kg, FTP 50–600 W, max HR 120–230 bpm);
- `ftp_pct` used when no user file or `--ftp` gives an FTP;
- out-of-range values: ERG targets above 2000 W (the NEO 2T's limit is 2200 W), `grade`
  outside -10…20, `resistance` outside 0…100;
- `text.at` beyond the end of its step.

It also prints a summary: the total time, and the expanded list of steps with each
repeat written out.

## Editing in VS Code (JSON Schema)

`schemas/tacx-workout.schema.json` and `schemas/tacx-user.schema.json` describe both
formats as JSON Schema (draft-07). VS Code's built-in JSON support uses them to give:
- completion for keys and values, plus snippets such as "Step: ramp", "Repeat block
  (on/off)", "New workout" and "New rider";
- hover descriptions for every field;
- red squiggles for wrong types, missing or unknown keys, out-of-range values, and steps
  without exactly one target.

A file names its schema in a `"$schema"` key, e.g.
`"$schema": "../schemas/tacx-workout.schema.json"` for a workout in `workouts\`. The
editor and the launcher's rider form write this key when they save. For files without
it, `.vscode/settings.json` maps `workouts/*.workout.json`, `user.json` and
`users/*.json` to the schemas.

A schema can't check everything. Message times past the end of their step, and watt
limits that depend on your FTP, are only checked by `tacx_neo.py check` and when the
workout loads.

## Importing Zwift `.zwo` files

ZWO is XML. Power values in it are fractions of FTP, and durations are in seconds. The
importer converts each element to the format above:

| ZWO element | Converted to |
|---|---|
| `<SteadyState Duration="300" Power="0.88"/>` | `{ "duration": 300, "ftp_pct": 88 }` |
| `<Warmup>` / `<Cooldown>` / `<Ramp Duration PowerLow PowerHigh>` | `{ "duration": d, "ftp_pct": [low, high] }` |
| `<IntervalsT Repeat OnDuration OffDuration OnPower OffPower>` | a `repeat` block holding an on step and an off step |
| `<FreeRide Duration>` | `{ "duration": d, "free": true }` |
| `Cadence` / `CadenceLow` / `CadenceHigh` attributes | `cadence` |
| `<textevent timeoffset message>` | `text` |

## Possible later additions

These aren't part of version 1; the `version` field leaves room for them.
- `hr_cap`: lower the ERG target while heart rate is above a limit. It could be given in
  bpm or as a % of `max_hr` from the user file.
- An FTP history in the user file (a list of dated test results), and the player
  suggesting an update after a hard workout.
- `until_lap`: hold a step until a key is pressed instead of for a fixed time.
- Distance-based steps in slope mode, e.g. "2 km at 6%".
