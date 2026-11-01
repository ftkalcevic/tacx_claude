"""
tacx_workout.py - user profile, structured workout files and the workout player.

File formats are described in workout_format.md:
  user.json          rider profile (name, weight, FTP, ...)
  *.workout.json     structured workout (steps, ramps, repeats)
  *.zwo              Zwift workouts, imported into the same model

WorkoutPlayer is a pure state machine: call tick() regularly with the current
cadence and it returns a trainer command whenever the target changes. drive()
runs it against a connected TacxNeo.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("tacx_workout")

MAX_ERG_W = 2000          # NEO 2T limit is 2200 W
INTENSITY_RANGE = (50, 150)


class WorkoutError(ValueError):
    pass


def parse_duration(v, where: str, allow_zero: bool = False) -> float:
    """Seconds as a number, or "M:SS" / "H:MM:SS"."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise WorkoutError(f"{where}: duration must be seconds or \"M:SS\", got {v!r}")
    if isinstance(v, str):
        parts = v.strip().split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise WorkoutError(f"{where}: can't read duration {v!r}")
        if not 1 <= len(nums) <= 3:
            raise WorkoutError(f"{where}: can't read duration {v!r}")
        d = 0.0
        for n in nums:
            d = d * 60 + n
    else:
        d = float(v)
    if d < 0 or (d == 0 and not allow_zero):
        raise WorkoutError(f"{where}: duration must be greater than 0")
    return d


def fmt_time(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _number(v, where: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise WorkoutError(f"{where}: expected a number, got {v!r}")
    if not lo <= v <= hi:
        raise WorkoutError(f"{where}: {v} is outside {lo:g}..{hi:g}")
    return float(v)


def _date(v, where: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(v)
    except (TypeError, ValueError):
        raise WorkoutError(f"{where}: expected a date \"YYYY-MM-DD\", got {v!r}")


def _check_keys(d: dict, allowed: set, where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise WorkoutError(f"{where}: unknown key(s) {', '.join(sorted(extra))}")


# --- User profile -------------------------------------------------------------
@dataclass
class UserProfile:
    name: str
    weight_kg: float
    ftp: float
    nickname: Optional[str] = None
    dob: Optional[datetime.date] = None
    sex: Optional[str] = None
    height_cm: Optional[float] = None
    ftp_tested: Optional[datetime.date] = None
    max_hr: Optional[int] = None
    resting_hr: Optional[int] = None
    bike_weight_kg: float = 8.0
    path: str = ""

    @property
    def display_name(self) -> str:
        return self.nickname or self.name

    @property
    def age(self) -> Optional[int]:
        if not self.dob:
            return None
        t = datetime.date.today()
        return t.year - self.dob.year - ((t.month, t.day) < (self.dob.month, self.dob.day))

    @property
    def est_max_hr(self) -> Optional[int]:
        return self.max_hr or (220 - self.age if self.age else None)


USER_KEYS = {"format", "version", "name", "nickname", "dob", "sex", "weight_kg", "height_cm",
             "ftp", "ftp_tested", "max_hr", "resting_hr", "bike_weight_kg"}


def find_user_file(path: Optional[str] = None) -> Optional[str]:
    if path:
        if not os.path.isfile(path):
            raise WorkoutError(f"user file not found: {path}")
        return path
    for cand in (os.path.join(os.getcwd(), "user.json"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "user.json")):
        if os.path.isfile(cand):
            return cand
    return None


def load_user(path: Optional[str] = None) -> Optional[UserProfile]:
    """Load the user file (see find_user_file for where it looks). None if there isn't one."""
    path = find_user_file(path)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except json.JSONDecodeError as e:
        raise WorkoutError(f"{path}: not valid JSON ({e})")
    w = os.path.basename(path)
    if not isinstance(d, dict) or d.get("format") != "tacx-user":
        raise WorkoutError(f'{w}: "format" must be "tacx-user"')
    if d.get("version") != 1:
        raise WorkoutError(f'{w}: unsupported "version" {d.get("version")!r} (expected 1)')
    _check_keys(d, USER_KEYS, w)
    for k in ("name", "weight_kg", "ftp"):
        if k not in d:
            raise WorkoutError(f'{w}: missing required "{k}"')
    if not isinstance(d["name"], str) or not d["name"].strip():
        raise WorkoutError(f'{w}.name: must be a non-empty string')
    u = UserProfile(name=d["name"].strip(),
                    weight_kg=_number(d["weight_kg"], f"{w}.weight_kg", 30, 200),
                    ftp=_number(d["ftp"], f"{w}.ftp", 50, 600), path=path)
    if "nickname" in d:
        u.nickname = str(d["nickname"])
    if "dob" in d:
        u.dob = _date(d["dob"], f"{w}.dob")
    if "sex" in d:
        if d["sex"] not in ("male", "female"):
            raise WorkoutError(f'{w}.sex: must be "male" or "female"')
        u.sex = d["sex"]
    if "height_cm" in d:
        u.height_cm = _number(d["height_cm"], f"{w}.height_cm", 100, 230)
    if "ftp_tested" in d:
        u.ftp_tested = _date(d["ftp_tested"], f"{w}.ftp_tested")
    if "max_hr" in d:
        u.max_hr = int(_number(d["max_hr"], f"{w}.max_hr", 120, 230))
    if "resting_hr" in d:
        u.resting_hr = int(_number(d["resting_hr"], f"{w}.resting_hr", 30, 100))
    if "bike_weight_kg" in d:
        u.bike_weight_kg = _number(d["bike_weight_kg"], f"{w}.bike_weight_kg", 3, 40)
    return u


# --- Workout model ------------------------------------------------------------
TARGET_KEYS = ("ftp_pct", "watts", "grade", "resistance", "free")
STEP_KEYS = {"name", "duration", "cadence", "text", *TARGET_KEYS}
REPEAT_KEYS = {"repeat", "steps", "name"}
WORKOUT_KEYS = {"format", "version", "name", "description", "author", "steps"}


@dataclass
class Step:
    name: str
    duration: float
    kind: str                       # one of TARGET_KEYS
    start: float                    # target at the start of the step
    end: float                      # target at the end (== start unless a ramp)
    cadence: Optional[tuple] = None  # (low, high) rpm
    texts: list = field(default_factory=list)  # [(at_seconds, message)]
    group: str = ""                 # repeat position, e.g. "VO2 2/5"
    where: str = ""                 # location in the file, for messages

    @property
    def is_erg(self) -> bool:
        return self.kind in ("ftp_pct", "watts")

    def value_at(self, t: float) -> float:
        f = min(1.0, max(0.0, t / self.duration))
        return self.start + (self.end - self.start) * f

    def watts_at(self, t: float, ftp: Optional[float]) -> Optional[float]:
        if self.kind == "watts":
            return self.value_at(t)
        if self.kind == "ftp_pct":
            return None if ftp is None else self.value_at(t) * ftp / 100
        return None

    def describe(self, ftp: Optional[float]) -> str:
        """Short target text, e.g. "275 W", "150-200 W", "4.0% slope"."""
        if self.is_erg:
            a, b = self.watts_at(0, ftp), self.watts_at(self.duration, ftp)
            if a is None:
                return f"{self.start:g}% FTP"
            return f"{a:.0f} W" if round(a) == round(b) else f"{a:.0f}-{b:.0f} W"
        if self.kind == "grade":
            return f"{self.start:.1f}% slope"
        if self.kind == "resistance":
            return f"{self.start:.0f}% resistance"
        return "free ride"


@dataclass
class Workout:
    name: str
    steps: list
    description: str = ""
    author: str = ""
    path: str = ""

    @property
    def total_s(self) -> float:
        return sum(s.duration for s in self.steps)

    @property
    def uses_ftp(self) -> bool:
        return any(s.kind == "ftp_pct" for s in self.steps)

    def summary(self, ftp: Optional[float]) -> str:
        lines = [f"{self.name}  ({fmt_time(self.total_s)}, {len(self.steps)} steps)"]
        t = 0.0
        for s in self.steps:
            label = s.name + (f"  [{s.group}]" if s.group else "")
            cad = "" if not s.cadence else f"  cadence {s.cadence[0]:.0f}-{s.cadence[1]:.0f}"
            lines.append(f"  {fmt_time(t):>8}  {fmt_time(s.duration):>6}  {s.describe(ftp):<18} {label}{cad}")
            t += s.duration
        return "\n".join(lines)


def _target(item: dict, where: str):
    present = [k for k in TARGET_KEYS if k in item]
    if len(present) != 1:
        raise WorkoutError(f"{where}: needs exactly one of {', '.join(TARGET_KEYS)}"
                           + (f" (found {', '.join(present)})" if present else ""))
    kind = present[0]
    v = item[kind]
    if kind == "free":
        if v is not True:
            raise WorkoutError(f'{where}.free: must be true')
        return kind, 0.0, 0.0
    ranges = {"ftp_pct": (0, 300), "watts": (0, MAX_ERG_W), "grade": (-10, 20), "resistance": (0, 100)}
    lo, hi = ranges[kind]
    if isinstance(v, list):
        if kind not in ("ftp_pct", "watts") or len(v) != 2:
            raise WorkoutError(f"{where}.{kind}: ramps are [start, end] and only for ftp_pct / watts")
        return kind, _number(v[0], f"{where}.{kind}[0]", lo, hi), _number(v[1], f"{where}.{kind}[1]", lo, hi)
    x = _number(v, f"{where}.{kind}", lo, hi)
    return kind, x, x


def _cadence(v, where: str) -> tuple:
    if isinstance(v, list):
        if len(v) != 2:
            raise WorkoutError(f"{where}.cadence: expected [low, high]")
        lo, hi = (_number(x, f"{where}.cadence", 20, 200) for x in v)
        if lo > hi:
            raise WorkoutError(f"{where}.cadence: low is above high")
        return lo, hi
    c = _number(v, f"{where}.cadence", 20, 200)
    return c - 5, c + 5


def parse_step(item: dict, where: str, group: str) -> Step:
    _check_keys(item, STEP_KEYS, where)
    if "duration" not in item:
        raise WorkoutError(f'{where}: missing "duration"')
    duration = parse_duration(item["duration"], f"{where}.duration")
    kind, a, b = _target(item, where)
    texts = []
    for j, t in enumerate(item.get("text", [])):
        tw = f"{where}.text[{j}]"
        if not isinstance(t, dict) or set(t) != {"at", "msg"}:
            raise WorkoutError(f'{tw}: expected {{"at": ..., "msg": ...}}')
        at = parse_duration(t["at"], f"{tw}.at", allow_zero=True)
        if at > duration:
            raise WorkoutError(f"{tw}.at: {fmt_time(at)} is past the end of the step ({fmt_time(duration)})")
        texts.append((at, str(t["msg"])))
    name = item.get("name") or {"ftp_pct": "Interval", "watts": "Interval", "grade": "Climb",
                                 "resistance": "Resistance", "free": "Free ride"}[kind]
    return Step(name=str(name), duration=duration, kind=kind, start=a, end=b,
                cadence=_cadence(item["cadence"], where) if "cadence" in item else None,
                texts=sorted(texts), group=group, where=where)


def _expand(items, where: str, groups: list, out: list) -> None:
    if not isinstance(items, list) or not items:
        raise WorkoutError(f"{where}: must be a non-empty list")
    for i, item in enumerate(items):
        w = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise WorkoutError(f"{w}: expected an object")
        if "repeat" in item:
            _check_keys(item, REPEAT_KEYS, w)
            n = item["repeat"]
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                raise WorkoutError(f"{w}.repeat: must be a whole number of 1 or more")
            label = item.get("name") or "Repeat"
            for k in range(n):
                _expand(item.get("steps"), f"{w}.steps", groups + [f"{label} {k + 1}/{n}"], out)
        else:
            out.append(parse_step(item, w, " · ".join(groups)))


def parse_workout(doc, path: str = "") -> Workout:
    w = os.path.basename(path) or "workout"
    if not isinstance(doc, dict) or doc.get("format") != "tacx-workout":
        raise WorkoutError(f'{w}: "format" must be "tacx-workout"')
    if doc.get("version") != 1:
        raise WorkoutError(f'{w}: unsupported "version" {doc.get("version")!r} (expected 1)')
    _check_keys(doc, WORKOUT_KEYS, w)
    if not isinstance(doc.get("name"), str) or not doc["name"].strip():
        raise WorkoutError(f'{w}: missing "name"')
    steps: list = []
    _expand(doc.get("steps"), "steps", [], steps)
    return Workout(name=doc["name"].strip(), steps=steps, description=doc.get("description", ""),
                   author=doc.get("author", ""), path=path)


def _zwo_items(path: str) -> dict:
    """Convert a Zwift .zwo file into the JSON workout structure."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise WorkoutError(f"{path}: not valid XML ({e})")
    wk = root.find("workout")
    if wk is None:
        raise WorkoutError(f"{path}: no <workout> element")
    pct = lambda x: round(float(x) * 100, 1)

    def common(el, d):
        out = {}
        a = el.attrib
        if "CadenceLow" in a and "CadenceHigh" in a:
            out["cadence"] = [float(a["CadenceLow"]), float(a["CadenceHigh"])]
        elif "Cadence" in a:
            out["cadence"] = float(a["Cadence"])
        texts = [{"at": min(float(t.get("timeoffset", 0)), d), "msg": t.get("message", "")}
                 for t in el if t.tag.lower() == "textevent"]
        if texts:
            out["text"] = texts
        return out

    items = []
    for el in wk:
        a, tag = el.attrib, el.tag
        d = float(a.get("Duration", 0))
        if tag == "SteadyState":
            items.append({"name": "Steady", "duration": d, "ftp_pct": pct(a["Power"]), **common(el, d)})
        elif tag in ("Warmup", "Cooldown", "Ramp"):
            name = {"Warmup": "Warm up", "Cooldown": "Cool down", "Ramp": "Ramp"}[tag]
            items.append({"name": name, "duration": d,
                          "ftp_pct": [pct(a["PowerLow"]), pct(a["PowerHigh"])], **common(el, d)})
        elif tag == "IntervalsT":
            on_d, off_d = float(a["OnDuration"]), float(a["OffDuration"])
            on = {"name": "On", "duration": on_d, "ftp_pct": pct(a["OnPower"]), **common(el, on_d)}
            off = {"name": "Off", "duration": off_d, "ftp_pct": pct(a["OffPower"])}
            if "CadenceResting" in a:
                off["cadence"] = float(a["CadenceResting"])
            items.append({"repeat": int(a.get("Repeat", 1)), "name": "Intervals", "steps": [on, off]})
        elif tag in ("FreeRide", "Freeride"):
            items.append({"name": "Free ride", "duration": d, "free": True, **common(el, d)})
        else:
            raise WorkoutError(f"{path}: unsupported ZWO element <{tag}>")
    return {"format": "tacx-workout", "version": 1,
            "name": (root.findtext("name") or os.path.splitext(os.path.basename(path))[0]).strip(),
            "description": (root.findtext("description") or "").strip(),
            "author": (root.findtext("author") or "").strip(), "steps": items}


def load_workout(path: str) -> Workout:
    if not os.path.isfile(path):
        raise WorkoutError(f"workout file not found: {path}")
    if path.lower().endswith(".zwo"):
        return parse_workout(_zwo_items(path), path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except json.JSONDecodeError as e:
        raise WorkoutError(f"{path}: not valid JSON ({e})")
    return parse_workout(doc, path)


# --- Player -------------------------------------------------------------------
class WorkoutPlayer:
    """
    Steps through a workout. Thread-safe: the GUI calls next_step() and
    set_intensity() while a driver thread or task calls tick().

    tick() returns a trainer command when the target changes:
      ("erg", watts) | ("grade", percent) | ("resistance", percent)
    """

    AUTOPAUSE_S = 3.0

    def __init__(self, workout: Workout, ftp: Optional[float], autopause: bool = True):
        if workout.uses_ftp and not ftp:
            raise WorkoutError("this workout uses ftp_pct targets but no FTP is set "
                               "(add a user.json or pass --ftp)")
        for s in workout.steps:
            for t in (0, s.duration):
                w = s.watts_at(t, ftp)
                if w is not None and w > MAX_ERG_W:
                    raise WorkoutError(f"{s.where}: {w:.0f} W is above the {MAX_ERG_W} W limit")
        self.workout = workout
        self.base_ftp = ftp
        self.intensity_pct = 100
        self.autopause = autopause
        self.starts = []
        t = 0.0
        for s in workout.steps:
            self.starts.append(t)
            t += s.duration
        self.index = 0
        self.step_elapsed = 0.0
        self.started = False
        self.paused = True
        self.finished = False
        self.message: Optional[str] = None
        self.message_time = 0.0
        self.trace: list = []           # (timeline seconds, actual watts)
        self._last_tick: Optional[float] = None
        self._last_pedal: Optional[float] = None
        self._sent = None
        self._sent_index = -1
        self._sent_time = 0.0
        self._force = True
        self._fired: set = set()
        self._lock = threading.RLock()

    # ----- controls (any thread) -----
    @property
    def ftp(self) -> Optional[float]:
        return None if self.base_ftp is None else self.base_ftp * self.intensity_pct / 100

    def set_intensity(self, pct: int) -> None:
        with self._lock:
            self.intensity_pct = int(min(INTENSITY_RANGE[1], max(INTENSITY_RANGE[0], pct)))
            self._force = True

    def resend(self) -> None:
        """Send the current target again on the next tick (e.g. after a failed write)."""
        with self._lock:
            self._force = True

    def next_step(self) -> None:
        with self._lock:
            if self.finished:
                return
            self._advance()
            self._force = True

    # ----- state -----
    @property
    def step(self) -> Optional[Step]:
        return None if self.finished else self.workout.steps[self.index]

    @property
    def position(self) -> float:
        """Seconds along the workout timeline (skips jump forward)."""
        if self.finished:
            return self.workout.total_s
        return self.starts[self.index] + self.step_elapsed

    def target_watts(self) -> Optional[float]:
        s = self.step
        return None if s is None else s.watts_at(self.step_elapsed, self.ftp)

    def _advance(self) -> None:
        self.index += 1
        self.step_elapsed = 0.0
        if self.index >= len(self.workout.steps):
            self.finished = True
            self.index = len(self.workout.steps) - 1
            self.message, self.message_time = "Workout complete!", time.time()

    def tick(self, now: float, cadence: Optional[float], power: Optional[float] = None):
        with self._lock:
            dt = 0.0 if self._last_tick is None else min(max(now - self._last_tick, 0.0), 2.0)
            self._last_tick = now
            if cadence:
                self._last_pedal = now
                self.started = True
            if self.autopause:
                self.paused = not self.started or now - self._last_pedal > self.AUTOPAUSE_S
            else:
                self.paused = False
            if not self.paused and not self.finished:
                self.step_elapsed += dt
                while not self.finished and self.step_elapsed >= self.workout.steps[self.index].duration:
                    over = self.step_elapsed - self.workout.steps[self.index].duration
                    self._advance()
                    if not self.finished:
                        self.step_elapsed = over
                if power is not None and not self.finished:
                    self.trace.append((self.position, power))
            s = self.step
            if s is not None:
                for j, (at, msg) in enumerate(s.texts):
                    if (self.index, j) not in self._fired and self.step_elapsed >= at:
                        self._fired.add((self.index, j))
                        self.message, self.message_time = msg, now
            return self._command(now)

    RAMP_UPDATE_S = 1.0

    def _command(self, now: float):
        s = self.step
        if s is None:
            cmd = ("grade", 0.0)            # workout over: back to a flat road
        elif s.is_erg:
            cmd = ("erg", int(round(s.watts_at(self.step_elapsed, self.ftp))))
        elif s.kind in ("grade", "resistance"):
            cmd = (s.kind, s.start)
        else:
            cmd = ("grade", 0.0)
        if cmd == self._sent and not self._force:
            return None
        # within a ramp, update at most once a second; step changes and overrides go at once
        same_step = self._sent_index == self.index and not self.finished
        if same_step and not self._force and now - self._sent_time < self.RAMP_UPDATE_S:
            return None
        self._force = False
        self._sent, self._sent_index, self._sent_time = cmd, self.index, now
        return cmd

    def snapshot(self) -> dict:
        """Everything a display needs, read consistently."""
        with self._lock:
            s = self.step
            nxt = None if self.finished or self.index + 1 >= len(self.workout.steps) \
                else self.workout.steps[self.index + 1]
            return {
                "step": s, "next": nxt, "index": self.index, "count": len(self.workout.steps),
                "step_elapsed": self.step_elapsed,
                "step_remaining": 0 if s is None else s.duration - self.step_elapsed,
                "position": self.position, "total": self.workout.total_s,
                "target_w": self.target_watts(), "paused": self.paused, "started": self.started,
                "finished": self.finished, "ftp": self.ftp, "base_ftp": self.base_ftp,
                "intensity": self.intensity_pct, "message": self.message,
                "message_age": time.time() - self.message_time,
                "trace": list(self.trace),
            }


def rider_inputs(state) -> tuple:
    """(cadence, power) from a TrainerState, or (None, None) when it's stale."""
    if state is None or time.time() - state.timestamp >= 3:
        return None, None
    cad = state.cadence_rpm if state.cadence_rpm is not None else state.crank_cadence_rpm
    power = state.power_w if state.power_w is not None else state.cps_power_w
    return cad, power


async def send_command(neo, cmd) -> None:
    kind, v = cmd
    if kind == "erg":
        await neo.set_target_power(v)
    elif kind == "grade":
        await neo.set_simulation(v)
    elif kind == "resistance":
        await neo.set_resistance(v)


async def drive(player: WorkoutPlayer, neo, stop: asyncio.Event, period: float = 0.5) -> None:
    """Run the player against a connected TacxNeo until `stop` is set."""
    while not stop.is_set():
        cad, power = rider_inputs(neo.state)
        cmd = player.tick(time.time(), cad, power)
        if cmd:
            try:
                await send_command(neo, cmd)
                log.info("workout: %s %s", *cmd)
            except Exception as e:
                log.error("workout: sending %s failed: %s", cmd, e)
                player.resend()
        try:
            await asyncio.wait_for(stop.wait(), period)
        except asyncio.TimeoutError:
            pass
