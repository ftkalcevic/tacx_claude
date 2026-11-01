"""
tacx_neo.py - Bluetooth LE interface for the Garmin Tacx NEO 2T Smart trainer.

The NEO 2T exposes three relevant BLE services:

  * Fitness Machine Service (FTMS, 0x1826)  - Indoor Bike Data + control point
                                               (ERG / resistance / simulation)
  * Cycling Power Service   (CPS, 0x1818)   - power, crank revs and the
                                               left/right pedal power balance
  * Tacx "FE-C over BLE"    (6E40FEC1-...)  - ANT+ FE-C pages tunnelled over BLE
                                               (alternative control path, plus
                                               trainer status / product info)

Per-pedal power: the NEO measures power at the flywheel/motor and derives the
left/right split from the torque profile over each crank revolution. It is
published as the CPS "Pedal Power Balance" field; this module turns that into
left_power / right_power watts and keeps per-leg statistics.

Requires:  pip install bleak
Only one app should control the trainer at a time - close Zwift / TrainerRoad /
Tacx Training before connecting.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # parsers remain usable without bleak (e.g. for tests)
    BleakClient = BleakScanner = None

log = logging.getLogger("tacx_neo")


def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# --- GATT UUIDs -------------------------------------------------------------
FTMS_SERVICE = _uuid16(0x1826)
FTMS_FEATURE = _uuid16(0x2ACC)
FTMS_INDOOR_BIKE_DATA = _uuid16(0x2AD2)
FTMS_TRAINING_STATUS = _uuid16(0x2AD3)
FTMS_RESISTANCE_RANGE = _uuid16(0x2AD6)
FTMS_POWER_RANGE = _uuid16(0x2AD8)
FTMS_CONTROL_POINT = _uuid16(0x2AD9)
FTMS_STATUS = _uuid16(0x2ADA)

CPS_SERVICE = _uuid16(0x1818)
CPS_MEASUREMENT = _uuid16(0x2A63)
CPS_FEATURE = _uuid16(0x2A65)
CPS_VECTOR = _uuid16(0x2A64)          # torque samples through the pedal stroke
CPS_CONTROL_POINT = _uuid16(0x2A66)
CPCP_REQUEST_SAMPLING_RATE = 0x0E
CPCP_RESPONSE = 0x20

HR_SERVICE = _uuid16(0x180D)
HR_MEASUREMENT = _uuid16(0x2A37)
HR_BODY_LOCATION = _uuid16(0x2A38)
BATTERY_LEVEL = _uuid16(0x2A19)
HR_LOCATIONS = {0: "other", 1: "chest", 2: "wrist", 3: "finger", 4: "hand", 5: "ear lobe", 6: "foot"}

DIS_CHARS = {
    "manufacturer": _uuid16(0x2A29),
    "model": _uuid16(0x2A24),
    "serial": _uuid16(0x2A25),
    "hardware_rev": _uuid16(0x2A27),
    "firmware_rev": _uuid16(0x2A26),
    "software_rev": _uuid16(0x2A28),
}

FEC_SERVICE = "6e40fec1-b5a3-f393-e0a9-e50e24dcca9e"
FEC_RX = "6e40fec2-b5a3-f393-e0a9-e50e24dcca9e"  # trainer -> us (notify)
FEC_TX = "6e40fec3-b5a3-f393-e0a9-e50e24dcca9e"  # us -> trainer (write)

# --- FTMS control point ----------------------------------------------------
OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_SET_TARGET_RESISTANCE = 0x04
OP_SET_TARGET_POWER = 0x05
OP_START_RESUME = 0x07
OP_STOP_PAUSE = 0x08
OP_SET_SIMULATION = 0x11
OP_SET_WHEEL_CIRCUMFERENCE = 0x12
OP_SPIN_DOWN = 0x13
OP_RESPONSE = 0x80

FTMS_RESULT = {
    0x01: "success",
    0x02: "op code not supported",
    0x03: "invalid parameter",
    0x04: "operation failed",
    0x05: "control not permitted",
}

FEC_STATE = {1: "asleep", 2: "ready", 3: "in use", 4: "finished/paused"}

SERVICE_NAMES = {
    FTMS_SERVICE: "FTMS", CPS_SERVICE: "Cycling Power", FEC_SERVICE: "Tacx FE-C",
    _uuid16(0x180A): "Device Info", _uuid16(0x1800): "GAP", _uuid16(0x1801): "GATT",
    _uuid16(0x1816): "Cycling Speed/Cadence", _uuid16(0x180F): "Battery",
}

CPS_FEATURE_BITS = [
    "pedal balance", "accumulated torque", "wheel revs", "crank revs",
    "extreme magnitudes", "extreme angles", "dead spot angles", "accumulated energy",
    "offset comp indicator", "offset comp", "content masking", "multiple locations",
    "crank length adj", "chain length adj", "chain weight adj", "span length adj",
]


def cps_features_str(cf: int) -> str:
    names = [n for i, n in enumerate(CPS_FEATURE_BITS) if cf & (1 << i)]
    names.append("torque-based" if cf & (1 << 16) else "force-based")
    return ", ".join(names)


class TrainerError(Exception):
    pass


# --- Data records -----------------------------------------------------------
@dataclass
class TrainerState:
    """Latest values from all notification sources, merged."""
    timestamp: float = 0.0
    # FTMS Indoor Bike Data
    speed_kmh: Optional[float] = None
    avg_speed_kmh: Optional[float] = None
    cadence_rpm: Optional[float] = None
    avg_cadence_rpm: Optional[float] = None
    distance_m: Optional[int] = None
    resistance_level: Optional[int] = None
    power_w: Optional[int] = None
    avg_power_w: Optional[int] = None
    energy_kcal: Optional[int] = None
    heart_rate_bpm: Optional[int] = None
    elapsed_s: Optional[int] = None
    # CPS
    cps_power_w: Optional[int] = None
    balance_left_pct: Optional[float] = None  # % of total from left pedal
    left_power_w: Optional[float] = None
    right_power_w: Optional[float] = None
    crank_revs: Optional[int] = None
    crank_cadence_rpm: Optional[float] = None
    accumulated_torque_nm: Optional[float] = None
    max_angle_deg: Optional[int] = None  # crank angle of max force (CPS extreme angles)
    min_angle_deg: Optional[int] = None  # crank angle of min force
    # FE-C
    fe_state: Optional[str] = None
    calibration_required: Optional[bool] = None
    user_config_required: Optional[bool] = None
    target_power_limit: Optional[str] = None


@dataclass
class SessionStats:
    """Running statistics since connect() or reset_stats()."""
    started: float = field(default_factory=time.time)
    samples: int = 0
    power_sum: float = 0.0
    max_power: float = 0.0
    left_sum: float = 0.0
    right_sum: float = 0.0
    balance_samples: int = 0
    max_left: float = 0.0
    max_right: float = 0.0
    cadence_sum: float = 0.0
    cadence_samples: int = 0
    max_cadence: float = 0.0
    max_speed: float = 0.0
    energy_j: float = 0.0
    hr_sum: float = 0.0
    hr_samples: int = 0
    max_hr: int = 0
    _last_t: float = 0.0

    def add_power(self, t: float, power: float) -> None:
        self.samples += 1
        self.power_sum += power
        self.max_power = max(self.max_power, power)
        if self._last_t:
            dt = t - self._last_t
            if 0 < dt < 5:
                self.energy_j += power * dt
        self._last_t = t

    def add_balance(self, left: float, right: float) -> None:
        self.balance_samples += 1
        self.left_sum += left
        self.right_sum += right
        self.max_left = max(self.max_left, left)
        self.max_right = max(self.max_right, right)

    def add_cadence(self, rpm: float) -> None:
        self.cadence_samples += 1
        self.cadence_sum += rpm
        self.max_cadence = max(self.max_cadence, rpm)

    def add_hr(self, bpm: int) -> None:
        if bpm > 0:
            self.hr_samples += 1
            self.hr_sum += bpm
            self.max_hr = max(self.max_hr, bpm)

    def summary(self) -> dict:
        avg = lambda s, n: round(s / n, 1) if n else None
        total_lr = self.left_sum + self.right_sum
        return {
            "duration_s": round(time.time() - self.started, 1),
            "avg_power_w": avg(self.power_sum, self.samples),
            "max_power_w": self.max_power,
            "avg_left_w": avg(self.left_sum, self.balance_samples),
            "avg_right_w": avg(self.right_sum, self.balance_samples),
            "max_left_w": self.max_left,
            "max_right_w": self.max_right,
            "avg_balance_left_pct": round(100 * self.left_sum / total_lr, 1) if total_lr else None,
            "avg_cadence_rpm": avg(self.cadence_sum, self.cadence_samples),
            "max_cadence_rpm": self.max_cadence,
            "max_speed_kmh": self.max_speed,
            "avg_heart_rate_bpm": avg(self.hr_sum, self.hr_samples),
            "max_heart_rate_bpm": self.max_hr or None,
            "work_kj": round(self.energy_j / 1000, 2),
        }


# --- Parsers (pure functions, no BLE needed) --------------------------------
def parse_indoor_bike_data(data: bytes) -> dict:
    """FTMS Indoor Bike Data (0x2AD2). Only fields present are returned."""
    flags = struct.unpack_from("<H", data, 0)[0]
    o = 2
    out = {}

    def take(fmt):
        nonlocal o
        v = struct.unpack_from(fmt, data, o)[0]
        o += struct.calcsize(fmt)
        return v

    if not flags & 0x0001:  # bit 0 "More Data" is inverted: 0 => speed present
        out["speed_kmh"] = take("<H") / 100
    if flags & 0x0002:
        out["avg_speed_kmh"] = take("<H") / 100
    if flags & 0x0004:
        out["cadence_rpm"] = take("<H") / 2
    if flags & 0x0008:
        out["avg_cadence_rpm"] = take("<H") / 2
    if flags & 0x0010:
        lo, hi = take("<H"), take("<B")
        out["distance_m"] = lo | (hi << 16)
    if flags & 0x0020:
        out["resistance_level"] = take("<h")
    if flags & 0x0040:
        out["power_w"] = take("<h")
    if flags & 0x0080:
        out["avg_power_w"] = take("<h")
    if flags & 0x0100:
        out["energy_kcal"] = take("<H")
        take("<H")  # energy per hour
        take("<B")  # energy per minute
    if flags & 0x0200:
        out["heart_rate_bpm"] = take("<B")
    if flags & 0x0400:
        take("<B")  # metabolic equivalent
    if flags & 0x0800:
        out["elapsed_s"] = take("<H")
    return out


def parse_cycling_power(data: bytes) -> dict:
    """CPS Cycling Power Measurement (0x2A63), incl. pedal power balance."""
    flags, power = struct.unpack_from("<Hh", data, 0)
    o = 4
    out = {"cps_power_w": power}

    if flags & 0x0001:
        raw = data[o] / 2  # 0.5 % resolution
        o += 1
        # bit 1: reference. 1 = value is left pedal %, 0 = unknown (Tacx sends left)
        left_pct = raw
        out["balance_left_pct"] = left_pct
        out["left_power_w"] = round(power * left_pct / 100, 1)
        out["right_power_w"] = round(power - out["left_power_w"], 1)
        out["balance_ref_known"] = bool(flags & 0x0002)
    if flags & 0x0004:
        out["accumulated_torque_nm"] = struct.unpack_from("<H", data, o)[0] / 32
        o += 2
    if flags & 0x0010:
        out["wheel_revs"], out["wheel_event_time"] = struct.unpack_from("<IH", data, o)
        o += 6
    if flags & 0x0020:
        out["crank_revs"], out["crank_event_time"] = struct.unpack_from("<HH", data, o)
        o += 4
    if flags & 0x0040:
        out["max_force_n"], out["min_force_n"] = struct.unpack_from("<hh", data, o)
        o += 4
    if flags & 0x0080:
        mx, mn = struct.unpack_from("<hh", data, o)
        out["max_torque_nm"], out["min_torque_nm"] = mx / 32, mn / 32
        o += 4
    if flags & 0x0100:
        v = data[o] | (data[o + 1] << 8) | (data[o + 2] << 16)
        out["max_angle_deg"], out["min_angle_deg"] = v & 0xFFF, v >> 12
        o += 3
    if flags & 0x0200:
        out["top_dead_spot_deg"] = struct.unpack_from("<H", data, o)[0]
        o += 2
    if flags & 0x0400:
        out["bottom_dead_spot_deg"] = struct.unpack_from("<H", data, o)[0]
        o += 2
    if flags & 0x0800:
        out["accumulated_energy_kj"] = struct.unpack_from("<H", data, o)[0]
    return out


def parse_power_vector(data: bytes) -> dict:
    """CPS Cycling Power Vector (0x2A64)."""
    flags = data[0]
    o = 1
    out = {"direction": (flags >> 4) & 0x3}
    if flags & 0x01:
        out["crank_revs"], out["crank_event_time"] = struct.unpack_from("<HH", data, o)
        o += 4
    if flags & 0x02:
        out["first_angle_deg"] = struct.unpack_from("<H", data, o)[0]
        o += 2
    n = (len(data) - o) // 2
    values = struct.unpack_from(f"<{n}h", data, o) if n else ()
    if flags & 0x08:
        out["torque_nm"] = [v / 32 for v in values]
    elif flags & 0x04:
        out["force_n"] = list(values)
    return out


@dataclass
class Revolution:
    """One crank revolution reconstructed from Cycling Power Vector samples."""
    timestamp: float
    rpm: float
    samples: int
    left_pct: float
    right_pct: float
    avg_torque_nm: float
    left_peak_nm: float
    left_peak_deg: int
    right_peak_nm: float
    right_peak_deg: int
    profile: list  # mean torque per 15 deg sector, 24 sectors from crank angle 0
    angles_deg: list = field(default_factory=list)  # crank angle of each torque sample
    torques_nm: list = field(default_factory=list)


class PedalStrokeAnalyzer:
    """
    Rebuilds each crank revolution from the NEO's Cycling Power Vector stream.

    The NEO sends, at the start of every revolution, a packet with the crank
    revolution count, crank event time and the crank angle of its first torque
    sample. Torque samples then follow at a fixed rate (40 Hz, from the CPS
    control point). Each sample's angle is first_angle + i * 360 / samples_per_rev.

    Torque in crank angles [offset, offset+180) is credited to one leg and the
    other half to the other leg; that's the same split the Tacx app shows as
    pedal balance. On the NEO 2T, 0-180 deg is the right leg's power stroke and
    180-360 the left's (`right_first=True`, confirmed by one-legged pedalling).
    """

    SECTORS = 24

    def __init__(self, sample_rate: float = 40.0, angle_offset: float = 0.0,
                 right_first: bool = True):
        self.sample_rate = sample_rate
        self.angle_offset = angle_offset
        self.right_first = right_first
        self._start: Optional[dict] = None   # revolution packet that started the current rev
        self._torque: list = []

    def feed(self, v: dict, now: float) -> Optional[Revolution]:
        """Add one parsed vector notification; returns a Revolution when one completes."""
        rev = None
        if "crank_revs" in v:
            if self._start is not None:
                rev = self._finish(v, now)
            self._start = v
            self._torque = []
        if self._start is not None:
            self._torque.extend(v.get("torque_nm", []))
        return rev

    def _finish(self, nxt: dict, now: float) -> Optional[Revolution]:
        s, tq = self._start, self._torque
        dr = (nxt["crank_revs"] - s["crank_revs"]) & 0xFFFF
        dt = ((nxt["crank_event_time"] - s["crank_event_time"]) & 0xFFFF) / 1024
        if dr != 1 or dt <= 0 or len(tq) < 8 or "first_angle_deg" not in s:
            return None  # coasting, dropped packets or stopped: skip this one
        step = 360.0 / len(tq)
        a0 = s["first_angle_deg"]
        halves = [0.0, 0.0]
        peaks = [(0.0, 0), (0.0, 0)]
        sums = [0.0] * self.SECTORS
        counts = [0] * self.SECTORS
        for i, t in enumerate(tq):
            ang = (a0 + i * step) % 360
            h = 0 if (ang - self.angle_offset) % 360 < 180 else 1
            halves[h] += t
            if t > peaks[h][0]:
                peaks[h] = (t, int(ang))
            k = int(ang // (360 / self.SECTORS))
            sums[k] += t
            counts[k] += 1
        total = halves[0] + halves[1]
        if total <= 0:
            return None
        r, l = (0, 1) if self.right_first else (1, 0)
        return Revolution(
            timestamp=now, rpm=round(60 / dt, 1), samples=len(tq),
            left_pct=round(100 * halves[l] / total, 1), right_pct=round(100 * halves[r] / total, 1),
            avg_torque_nm=round(sum(tq) / len(tq), 2),
            left_peak_nm=round(peaks[l][0], 1), left_peak_deg=peaks[l][1],
            right_peak_nm=round(peaks[r][0], 1), right_peak_deg=peaks[r][1],
            profile=[round(sums[k] / counts[k], 1) if counts[k] else None for k in range(self.SECTORS)],
            angles_deg=[(a0 + i * step) % 360 for i in range(len(tq))],
            torques_nm=list(tq),
        )


def profile_bar(profile: list) -> str:
    """ASCII bar of the torque profile, one character per 15 deg sector."""
    ramp = " .:-=+*#%@"
    vals = [v or 0 for v in profile]
    top = max(vals) or 1
    return "".join(ramp[min(len(ramp) - 1, max(0, int(v / top * (len(ramp) - 1) + 0.5)))] for v in vals)


def fec_build(payload: bytes, channel: int = 5) -> bytes:
    """Wrap an 8-byte ANT+ FE-C page in an ANT broadcast message frame."""
    if len(payload) != 8:
        raise ValueError("FE-C payload must be 8 bytes")
    msg = bytes([0xA4, 0x09, 0x4E, channel]) + payload
    chk = 0
    for b in msg:
        chk ^= b
    return msg + bytes([chk])


def fec_split(data: bytes) -> list[bytes]:
    """Extract 8-byte page payloads from one or more ANT frames."""
    pages, i = [], 0
    while i + 4 <= len(data):
        if data[i] != 0xA4:
            i += 1
            continue
        n = data[i + 1]
        frame = data[i:i + n + 4]
        if len(frame) < n + 4:
            break
        chk = 0
        for b in frame[:-1]:
            chk ^= b
        if chk == frame[-1] and frame[2] == 0x4E and n == 9:
            pages.append(frame[4:12])
        i += n + 4
    return pages


def parse_fec_page(p: bytes) -> dict:
    page = p[0]
    out = {"page": page}
    if page == 16:  # General FE data
        out["equipment_type"] = p[1]
        out["fec_elapsed_raw"] = p[2]   # 0.25 s, rolls over at 64 s
        out["fec_distance_raw"] = p[3]  # m, rolls over at 256 m
        out["fec_speed_kmh"] = round(struct.unpack_from("<H", p, 4)[0] * 0.0036, 2)
        if p[6] != 0xFF:
            out["heart_rate_bpm"] = p[6]
        out["fe_state"] = FEC_STATE.get((p[7] >> 4) & 0x7, "unknown")
    elif page == 17:  # General settings
        out["cycle_length_m"] = p[3] / 100
        out["incline_pct"] = struct.unpack_from("<h", p, 4)[0] / 100
        out["resistance_pct"] = p[6] / 2
    elif page == 25:  # Specific trainer data
        out["fec_cadence_rpm"] = p[2]
        out["fec_accum_power_w"] = struct.unpack_from("<H", p, 3)[0]
        out["fec_power_w"] = p[5] | ((p[6] & 0x0F) << 8)
        status = p[6] >> 4
        out["calibration_required"] = bool(status & 0x3)
        out["user_config_required"] = bool(status & 0x4)
        out["target_power_limit"] = {0: "at target", 1: "too low", 2: "too high", 3: "undetermined"}[p[7] & 0x3]
        out["fe_state"] = FEC_STATE.get((p[7] >> 4) & 0x7, "unknown")
    elif page == 54:  # FE capabilities
        out["max_resistance_n"] = struct.unpack_from("<H", p, 5)[0]
        out["capabilities"] = p[7]
    elif page == 55:  # User configuration (echo)
        out["user_weight_kg"] = struct.unpack_from("<H", p, 1)[0] / 100
        out["bike_weight_kg"] = ((p[4] >> 4) | (p[5] << 4)) * 0.05
        out["wheel_diameter_m"] = p[6] / 100
    elif page == 80:  # Manufacturer identification
        out["hw_revision"] = p[3]
        out["manufacturer_id"] = struct.unpack_from("<H", p, 4)[0]
        out["model_number"] = struct.unpack_from("<H", p, 6)[0]
    elif page == 81:  # Product information
        out["sw_revision"] = f"{p[3]}.{p[2] if p[2] != 0xFF else 0}"
        out["serial_number"] = struct.unpack_from("<I", p, 4)[0]
    else:
        out["raw"] = p.hex()
    return out


def parse_heart_rate(data: bytes) -> dict:
    """Heart Rate Measurement (0x2A37)."""
    flags = data[0]
    if flags & 0x01:
        hr, o = struct.unpack_from("<H", data, 1)[0], 3
    else:
        hr, o = data[1], 2
    out = {"heart_rate_bpm": hr}
    if flags & 0x04:  # contact detection supported
        out["sensor_contact"] = bool(flags & 0x02)
    if flags & 0x08:
        out["hr_energy_kj"] = struct.unpack_from("<H", data, o)[0]
        o += 2
    if flags & 0x10:
        n = (len(data) - o) // 2
        out["rr_ms"] = [round(v * 1000 / 1024) for v in struct.unpack_from(f"<{n}H", data, o)]
    return out


# --- Heart rate monitor -----------------------------------------------------
class HeartRateMonitor:
    """
    Bluetooth heart rate strap (standard Heart Rate Service), e.g. Garmin HRM 200.

    run() keeps the strap connected until `stop` is set: it scans, connects,
    and reconnects after drop-outs. Garmin straps only advertise while worn
    (they need skin contact), and allow two Bluetooth connections at once.

        hrm = HeartRateMonitor()
        hrm.on_update = lambda f: print(f["heart_rate_bpm"])
        await hrm.run(stop_event)
    """

    def __init__(self, address: Optional[str] = None, name_prefix: Optional[str] = None,
                 timeout: float = 10.0):
        if BleakClient is None:
            raise RuntimeError("bleak is not installed: pip install bleak")
        self.address = address
        self.name_prefix = name_prefix
        self.timeout = timeout
        self.name: Optional[str] = None
        self.connected = False
        self.heart_rate: Optional[int] = None
        self.last_time = 0.0
        self.rr_ms: list = []
        self.battery_pct: Optional[int] = None
        self.location: Optional[str] = None
        self.status = "HRM: searching..."
        self.on_update: Optional[Callable[[dict], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None

    def _set_status(self, s: str) -> None:
        self.status = s
        log.info(s)
        if self.on_status:
            self.on_status(s)

    def _matches(self, dev, adv) -> bool:
        if self.address:
            return dev.address.upper() == self.address.upper()
        name = adv.local_name or dev.name or ""
        if self.name_prefix and not name.lower().startswith(self.name_prefix.lower()):
            return False
        return HR_SERVICE in [u.lower() for u in adv.service_uuids]

    def _on_measurement(self, _c, data: bytearray) -> None:
        f = parse_heart_rate(bytes(data))
        self.heart_rate = f["heart_rate_bpm"]
        self.last_time = time.time()
        self.rr_ms = f.get("rr_ms", [])
        if self.on_update:
            self.on_update(f)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            dev = await BleakScanner.find_device_by_filter(self._matches, timeout=self.timeout)
            if dev is None:
                self._set_status("HRM: not found (is the strap on? it only broadcasts when worn)")
                continue
            gone = asyncio.Event()
            try:
                async with BleakClient(dev, timeout=self.timeout,
                                       disconnected_callback=lambda _c: gone.set()) as client:
                    self.name = dev.name or dev.address
                    if client.services.get_characteristic(HR_BODY_LOCATION):
                        loc = await client.read_gatt_char(HR_BODY_LOCATION)
                        self.location = HR_LOCATIONS.get(loc[0], "other")
                    if client.services.get_characteristic(BATTERY_LEVEL):
                        self.battery_pct = (await client.read_gatt_char(BATTERY_LEVEL))[0]
                    await client.start_notify(HR_MEASUREMENT, self._on_measurement)
                    self.connected = True
                    batt = "" if self.battery_pct is None else f", battery {self.battery_pct}%"
                    self._set_status(f"HRM: {self.name}{batt}")
                    stop_wait = asyncio.create_task(stop.wait())
                    gone_wait = asyncio.create_task(gone.wait())
                    await asyncio.wait({stop_wait, gone_wait}, return_when=asyncio.FIRST_COMPLETED)
                    stop_wait.cancel()
                    gone_wait.cancel()
            except Exception as e:
                self._set_status(f"HRM: connection failed ({e})")
            self.connected = False
            self.heart_rate = None
            if not stop.is_set():
                self._set_status("HRM: disconnected, reconnecting...")
                await asyncio.sleep(2)


# --- Trainer ----------------------------------------------------------------
class TacxNeo:
    """
    Async interface to a Tacx NEO 2T.

        async with TacxNeo() as neo:          # scans for "Tacx Neo..." and connects
            neo.on_update = print             # called with TrainerState on every notification
            await neo.set_target_power(200)   # ERG mode
            await asyncio.sleep(60)
            print(neo.stats.summary())

    control="ftms" uses the FTMS control point; control="fec" sends ANT+ FE-C
    pages over the Tacx BLE service instead. control="auto" (default) picks FTMS
    if the trainer has it, else FE-C (NEO 2T firmware 2.7.0 has no FTMS).
    """

    def __init__(self, address: Optional[str] = None, name_prefix: str = "Tacx Neo",
                 control: str = "auto", timeout: float = 10.0):
        if BleakClient is None:
            raise RuntimeError("bleak is not installed: pip install bleak")
        self.address = address
        self.name_prefix = name_prefix
        self.control = control
        self.timeout = timeout
        self.client: Optional[BleakClient] = None
        self.state = TrainerState()
        self.stats = SessionStats()
        self.fec_info: dict = {}
        self.on_update: Optional[Callable[[TrainerState], None]] = None
        self.on_raw: Optional[Callable[[str, bytes], None]] = None  # (source, bytes)
        self._cp_waiter: Optional[asyncio.Future] = None
        self._cp_lock = asyncio.Lock()
        self._has_control = False
        self._hrm: Optional[HeartRateMonitor] = None
        self._last_crank: Optional[tuple[int, int]] = None
        self._ftms_data = False
        self._fec_prev: Optional[tuple[int, int]] = None  # (elapsed_raw, distance_raw)
        self._fec_elapsed = 0.0
        self._fec_distance = 0
        self.stroke = PedalStrokeAnalyzer()
        self.on_revolution: Optional[Callable[[Revolution], None]] = None
        self.last_revolution: Optional[Revolution] = None

    # ----- connection -----
    @staticmethod
    async def scan(timeout: float = 8.0, name_prefix: str = "Tacx", all_devices: bool = False) -> list:
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        found = []
        for dev, adv in devices.values():
            name = adv.local_name or dev.name or ""
            uuids = [u.lower() for u in adv.service_uuids]
            if HR_SERVICE in uuids:
                kind = "heart rate"
            elif name.lower().startswith(name_prefix.lower()) or FTMS_SERVICE in uuids or FEC_SERVICE in uuids:
                kind = "trainer"
            elif all_devices:
                kind = ""
            else:
                continue
            found.append((dev.address, name, adv.rssi, kind))
        return sorted(found, key=lambda d: -d[2])

    async def connect(self) -> None:
        if not self.address:
            log.info("Scanning for %s...", self.name_prefix)
            dev = await BleakScanner.find_device_by_filter(
                lambda d, a: (a.local_name or d.name or "").lower().startswith(self.name_prefix.lower()),
                timeout=self.timeout)
            if dev is None:
                raise TrainerError(f"No device starting with '{self.name_prefix}' found")
            self.address = dev.address
        self.client = BleakClient(self.address, timeout=self.timeout)
        await self.client.connect()
        log.info("Connected to %s", self.address)

        svcs = self.client.services
        self._has = lambda uuid: svcs.get_characteristic(uuid) is not None
        if self._has(FTMS_INDOOR_BIKE_DATA):
            await self.client.start_notify(FTMS_INDOOR_BIKE_DATA, self._on_bike_data)
        if self._has(CPS_MEASUREMENT):
            await self.client.start_notify(CPS_MEASUREMENT, self._on_cps)
        if self._has(FTMS_CONTROL_POINT):
            await self.client.start_notify(FTMS_CONTROL_POINT, self._on_cp)
        if self._has(FTMS_STATUS):
            await self.client.start_notify(FTMS_STATUS, self._on_ftms_status)
        if self._has(FEC_RX):
            await self.client.start_notify(FEC_RX, self._on_fec)
        if self._has(CPS_VECTOR):
            # Same sequence as the Tacx app: vector notify, control point indicate,
            # then Request Sampling Rate (the NEO answers 40 Hz).
            await self.client.start_notify(CPS_VECTOR, self._on_vector)
            if self._has(CPS_CONTROL_POINT):
                await self.client.start_notify(CPS_CONTROL_POINT, self._on_cpcp)
                await self.client.write_gatt_char(CPS_CONTROL_POINT,
                                                  bytes([CPCP_REQUEST_SAMPLING_RATE]), response=True)
        self._ftms_data = self._has(FTMS_INDOOR_BIKE_DATA)
        if self.control == "auto":
            self.control = "ftms" if self._has(FTMS_CONTROL_POINT) else "fec"
            log.info("Using %s control", self.control.upper())
        self.reset_stats()

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self._has_control = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    def reset_stats(self) -> None:
        self.stats = SessionStats()

    # ----- device information -----
    async def read_device_info(self) -> dict:
        info = {}
        for key, uuid in DIS_CHARS.items():
            if self._has(uuid):
                try:
                    info[key] = (await self.client.read_gatt_char(uuid)).decode(errors="replace").strip("\x00 ")
                except Exception as e:  # some chars may not be readable
                    log.debug("read %s failed: %s", key, e)
        if self._has(FTMS_RESISTANCE_RANGE):
            lo, hi, inc = struct.unpack("<hhH", await self.client.read_gatt_char(FTMS_RESISTANCE_RANGE))
            info["resistance_range"] = (lo / 10, hi / 10, inc / 10)
        if self._has(FTMS_POWER_RANGE):
            info["power_range_w"] = struct.unpack("<hhH", await self.client.read_gatt_char(FTMS_POWER_RANGE))
        if self._has(FTMS_FEATURE):
            mf, tf = struct.unpack("<II", (await self.client.read_gatt_char(FTMS_FEATURE))[:8])
            info["ftms_machine_features"] = f"0x{mf:08x}"
            info["ftms_target_features"] = f"0x{tf:08x}"
        if self._has(CPS_FEATURE):
            cf = struct.unpack("<I", await self.client.read_gatt_char(CPS_FEATURE))[0]
            info["cps_features"] = f"0x{cf:08x} ({cps_features_str(cf)})"
            info["pedal_balance_supported"] = bool(cf & 0x1)
        info["ble_services"] = ", ".join(SERVICE_NAMES.get(s.uuid.lower(), s.uuid)
                                         for s in self.client.services)
        info["control"] = self.control
        if self._has(FEC_TX):
            for page in (80, 81, 54):
                await self.fec_request_page(page)
            await asyncio.sleep(1.5)
            info.update({k: v for k, v in self.fec_info.items() if k != "page"})
        return info

    # ----- notification handlers -----
    def attach_hrm(self, hrm: "HeartRateMonitor") -> None:
        """Route a heart rate strap into this trainer's state, stats and callbacks.
        While attached, heart rate forwarded by the trainer itself is ignored."""
        self._hrm = hrm
        hrm.on_update = lambda f: self._emit("hrm", b"", f)

    def _emit(self, source: str, data: bytes, fields: dict) -> None:
        if source != "hrm" and self._hrm and "heart_rate_bpm" in fields:
            fields = {k: v for k, v in fields.items() if k != "heart_rate_bpm"}
        now = time.time()
        self.state.timestamp = now
        for k, v in fields.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        if source != "vector":
            self._update_split(now)
        if "power_w" in fields:
            self.stats.add_power(now, fields["power_w"])
        if "cadence_rpm" in fields:
            self.stats.add_cadence(fields["cadence_rpm"])
        if "speed_kmh" in fields:
            self.stats.max_speed = max(self.stats.max_speed, fields["speed_kmh"])
        if "heart_rate_bpm" in fields:
            self.stats.add_hr(fields["heart_rate_bpm"])
        if "left_power_w" in fields:
            self.stats.add_balance(fields["left_power_w"], fields["right_power_w"])
        if self.on_raw:
            self.on_raw(source, data)
        if self.on_update:
            self.on_update(self.state)

    def _on_bike_data(self, _c, data: bytearray) -> None:
        self._emit("ftms", bytes(data), parse_indoor_bike_data(data))

    def _on_cps(self, _c, data: bytearray) -> None:
        f = parse_cycling_power(data)
        if "crank_revs" in f:
            revs, t = f["crank_revs"], f["crank_event_time"]
            if self._last_crank:
                dr = (revs - self._last_crank[0]) & 0xFFFF
                dt = ((t - self._last_crank[1]) & 0xFFFF) / 1024
                if dt > 0:
                    f["crank_cadence_rpm"] = round(60 * dr / dt, 1)
                elif dr == 0 and dt == 0 and f["cps_power_w"] == 0:
                    f["crank_cadence_rpm"] = 0.0
            self._last_crank = (revs, t)
        self._emit("cps", bytes(data), f)

    def _on_fec(self, _c, data: bytearray) -> None:
        for p in fec_split(bytes(data)):
            f = parse_fec_page(p)
            if f["page"] in (54, 55, 80, 81):
                self.fec_info.update(f)
            if not self._ftms_data:
                self._fec_to_state(f)
            self._emit("fec", p, f)

    def _fec_to_state(self, f: dict) -> None:
        """Without FTMS Indoor Bike Data, FE-C pages are the source of speed etc."""
        if f["page"] == 16:
            f["speed_kmh"] = f["fec_speed_kmh"]
            cur = (f["fec_elapsed_raw"], f["fec_distance_raw"])
            if self._fec_prev:
                self._fec_elapsed += ((cur[0] - self._fec_prev[0]) & 0xFF) * 0.25
                self._fec_distance += (cur[1] - self._fec_prev[1]) & 0xFF
            self._fec_prev = cur
            f["elapsed_s"] = int(self._fec_elapsed)
            f["distance_m"] = self._fec_distance
        elif f["page"] == 25:
            f["cadence_rpm"] = f["fec_cadence_rpm"]
            f["power_w"] = f["fec_power_w"]

    def _on_vector(self, _c, data: bytearray) -> None:
        rev = self.stroke.feed(parse_power_vector(bytes(data)), time.time())
        if rev is None:
            if self.on_raw:
                self.on_raw("vector", bytes(data))
            return
        self.last_revolution = rev
        power = self.state.power_w if self.state.power_w is not None else self.state.cps_power_w
        f = {"balance_left_pct": rev.left_pct}
        if power is not None:
            f["left_power_w"] = round(power * rev.left_pct / 100, 1)
            f["right_power_w"] = round(power - f["left_power_w"], 1)
        if self.on_revolution:
            self.on_revolution(rev)
        self._emit("vector", bytes(data), f)

    def _update_split(self, now: float) -> None:
        """Re-apply the last revolution's balance to the current power; clear it once stale."""
        rev = self.last_revolution
        if rev is None:
            return
        power = self.state.power_w if self.state.power_w is not None else self.state.cps_power_w
        if now - rev.timestamp > max(3.0, 2 * 60 / max(rev.rpm, 1)) or not power:
            self.state.balance_left_pct = self.state.left_power_w = self.state.right_power_w = None
            return
        self.state.balance_left_pct = rev.left_pct
        self.state.left_power_w = round(power * rev.left_pct / 100, 1)
        self.state.right_power_w = round(power - self.state.left_power_w, 1)

    def _on_cpcp(self, _c, data: bytearray) -> None:
        if len(data) >= 4 and data[0] == CPCP_RESPONSE and data[1] == CPCP_REQUEST_SAMPLING_RATE \
                and data[2] == 0x01:
            self.stroke.sample_rate = data[3]
            log.info("Cycling Power Vector sampling rate %d Hz", data[3])

    def _on_ftms_status(self, _c, data: bytearray) -> None:
        log.debug("FTMS status: %s", bytes(data).hex())

    def _on_cp(self, _c, data: bytearray) -> None:
        if len(data) >= 3 and data[0] == OP_RESPONSE and self._cp_waiter and not self._cp_waiter.done():
            self._cp_waiter.set_result((data[1], data[2]))

    # ----- FTMS control -----
    async def _cp_write(self, opcode: int, params: bytes = b"") -> None:
        async with self._cp_lock:
            loop = asyncio.get_running_loop()
            self._cp_waiter = loop.create_future()
            await self.client.write_gatt_char(FTMS_CONTROL_POINT, bytes([opcode]) + params, response=True)
            try:
                op, result = await asyncio.wait_for(self._cp_waiter, 3.0)
            except asyncio.TimeoutError:
                raise TrainerError(f"No response to FTMS op 0x{opcode:02x}")
            if op != opcode or result != 0x01:
                raise TrainerError(f"FTMS op 0x{opcode:02x}: {FTMS_RESULT.get(result, hex(result))}")

    async def _ensure_control(self) -> None:
        if not self._has(FTMS_CONTROL_POINT):
            raise TrainerError("This trainer has no FTMS control point - use control='fec'")
        if not self._has_control:
            await self._cp_write(OP_REQUEST_CONTROL)
            self._has_control = True

    async def _ctl(self, opcode: int, params: bytes = b"") -> None:
        await self._ensure_control()
        await self._cp_write(opcode, params)

    # ----- public settings API -----
    async def set_target_power(self, watts: int) -> None:
        """ERG mode: hold a constant power regardless of cadence."""
        if self.control == "fec":
            q = max(0, min(4000, int(watts))) * 4  # 0.25 W
            await self.fec_send(bytes([49, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, q & 0xFF, q >> 8]))
        else:
            await self._ctl(OP_SET_TARGET_POWER, struct.pack("<h", int(watts)))

    async def set_resistance(self, percent: float) -> None:
        """Basic resistance (load) mode, 0-100 %."""
        percent = max(0.0, min(100.0, percent))
        if self.control == "fec":
            await self.fec_send(bytes([48] + [0xFF] * 6 + [int(round(percent * 2))]))
        else:
            # FTMS 1.0: UINT8, resolution 0.1 -> 0..100 % maps to 0..200 (0.5 % steps)
            await self._ctl(OP_SET_TARGET_RESISTANCE, bytes([int(round(percent * 2))]))

    async def set_simulation(self, grade_pct: float = 0.0, wind_speed_ms: float = 0.0,
                             crr: float = 0.004, cw: float = 0.51) -> None:
        """
        Simulation (slope) mode.
          grade_pct     road gradient in % (-200..200, the NEO handles ~-15..25)
          wind_speed_ms head wind in m/s (negative = tail wind)
          crr           rolling resistance coefficient
          cw            wind resistance coefficient (kg/m) = 0.5 * rho * CdA
        """
        if self.control == "fec":
            await self.fec_send(bytes([50, 0xFF, 0xFF, 0xFF, 0xFF,
                                       max(0, min(186, int(round(cw * 100)))),
                                       int(max(-127, min(127, round(wind_speed_ms * 3.6)))) & 0xFF,
                                       100]))
            g = int(round((grade_pct + 200) * 100))
            await self.fec_send(bytes([51, 0xFF, 0xFF, 0xFF, 0xFF, g & 0xFF, g >> 8,
                                       max(0, min(254, int(round(crr / 5e-5))))]))
        else:
            await self._ctl(OP_SET_SIMULATION, struct.pack(
                "<hhBB", int(round(wind_speed_ms * 1000)), int(round(grade_pct * 100)),
                max(0, min(255, int(round(crr * 10000)))), max(0, min(255, int(round(cw * 100))))))

    async def set_grade(self, grade_pct: float) -> None:
        await self.set_simulation(grade_pct=grade_pct)

    async def set_wheel_circumference(self, mm: float) -> None:
        await self._ctl(OP_SET_WHEEL_CIRCUMFERENCE, struct.pack("<H", int(round(mm * 10))))

    async def set_user_config(self, user_kg: float, bike_kg: float = 8.0,
                              wheel_diameter_m: float = 0.7) -> None:
        """Rider/bike weight (FE-C page 55) - affects simulation-mode physics."""
        u = int(round(user_kg * 100))
        b = int(round(bike_kg / 0.05)) & 0xFFF
        d = int(round(wheel_diameter_m * 100))
        await self.fec_send(bytes([55, u & 0xFF, u >> 8, 0xFF, (b & 0x0F) << 4, b >> 4, d, 0x00]))

    async def start(self) -> None:
        await self._ctl(OP_START_RESUME)

    async def stop(self, pause: bool = False) -> None:
        await self._ctl(OP_STOP_PAUSE, bytes([0x02 if pause else 0x01]))

    async def reset(self) -> None:
        await self._ctl(OP_RESET)
        self._has_control = False

    # ----- FE-C over BLE -----
    async def fec_send(self, payload: bytes) -> None:
        if not self._has(FEC_TX):
            raise TrainerError("Trainer does not expose the Tacx FE-C BLE service")
        await self.client.write_gatt_char(FEC_TX, fec_build(payload), response=None)

    async def fec_request_page(self, page: int, times: int = 2) -> None:
        """FE-C page 70 'Request Data Page'."""
        await self.fec_send(bytes([70, 0xFF, 0xFF, 0xFF, 0xFF, 0x80 | times, page, 0x01]))


# --- CLI --------------------------------------------------------------------
def _fmt(s: TrainerState) -> str:
    f = lambda v, fmt="{:.0f}": "-" if v is None else fmt.format(v)
    cad = s.cadence_rpm if s.cadence_rpm is not None else s.crank_cadence_rpm
    return (f"P {f(s.power_w if s.power_w is not None else s.cps_power_w):>4} W  "
            f"L {f(s.left_power_w):>4} W  R {f(s.right_power_w):>4} W  "
            f"bal {f(s.balance_left_pct, '{:.1f}'):>5}%L  "
            f"cad {f(cad):>3} rpm  spd {f(s.speed_kmh, '{:.1f}'):>5} km/h  "
            f"dist {f(s.distance_m):>5} m  hr {f(s.heart_rate_bpm):>3} bpm  "
            f"force max/min @ {f(s.max_angle_deg)}/{f(s.min_angle_deg)} deg  "
            f"state {s.fe_state or '-'}")


def _start_ant(args):
    from tacx_ant import TacxNeoAnt, format_stroke

    ant = TacxNeoAnt(args.ant_device)
    last = [0.0]

    def show(s):
        if s.timestamp - last[0] >= 1.0:
            last[0] = s.timestamp
            print(format_stroke(s), flush=True)

    ant.on_update = show
    if args.raw:
        ant.on_raw = lambda b: print(f"  [ant] {b.hex()}")
    ant.start()
    return ant


async def _sleep(seconds: float) -> None:
    try:
        await asyncio.sleep(seconds if seconds else 10 ** 9)
    except asyncio.CancelledError:
        pass


async def _main(args) -> None:
    if args.cmd == "scan":
        print(f"Scanning for {args.timeout:.0f} s...", flush=True)
        found = await TacxNeo.scan(args.timeout, all_devices=args.all)
        for addr, name, rssi, kind in found:
            print(f"{addr}  {name:<24} RSSI {rssi:<5} {kind}")
        if not any(k == "trainer" for *_, k in found):
            print("No trainers found. Make sure the NEO is powered and awake (pedal to wake it), "
                  "and that no other app (Zwift, Tacx app, a phone or head unit) is connected - "
                  "it stops advertising while connected. Use --all to list every BLE device.")
        return

    if args.cmd == "hrm":
        hrm = HeartRateMonitor(args.hrm_address)

        def show(f):
            rr = f" RR {' '.join(map(str, f['rr_ms']))} ms" if f.get("rr_ms") else ""
            contact = "" if f.get("sensor_contact", True) else "  (no skin contact)"
            print(f"HR {f['heart_rate_bpm']:3d} bpm{rr}{contact}", flush=True)

        hrm.on_update = show
        stop = asyncio.Event()
        task = asyncio.create_task(hrm.run(stop))
        try:
            await _sleep(args.seconds)
        finally:
            stop.set()
            task.cancel()
        return

    ant = _start_ant(args) if args.cmd == "ant" or getattr(args, "ant", False) else None
    try:
        if args.cmd == "ant":
            await _sleep(args.seconds)
        else:
            await _ble_session(args)
    finally:
        if ant:
            ant.stop()
            print("\nANT+ pedal stroke summary:")
            for k, v in {**ant.info, **ant.stats.summary()}.items():
                print(f"{k:>22}: {v}")


def _fmt_rev(r: Revolution) -> str:
    return (f"  rev {r.rpm:5.1f} rpm  L {r.left_pct:4.1f}% R {r.right_pct:4.1f}%  "
            f"peak L {r.left_peak_nm:4.1f} Nm @{r.left_peak_deg:3d}  R {r.right_peak_nm:4.1f} Nm @{r.right_peak_deg:3d}  "
            f"torque 0-360 |{profile_bar(r.profile)}|")


async def _ble_session(args) -> None:
    async with TacxNeo(args.address, control=args.control) as neo:
        if args.cmd == "info":
            for k, v in (await neo.read_device_info()).items():
                print(f"{k:>24}: {v}")
            return

        if args.weight:
            await neo.set_user_config(args.weight, args.bike_weight)
        if args.cmd == "erg":
            await neo.set_target_power(args.watts)
        elif args.cmd == "resistance":
            await neo.set_resistance(args.percent)
        elif args.cmd == "sim":
            await neo.set_simulation(args.grade, args.wind, args.crr, args.cw)

        last = [0.0]

        def show(s: TrainerState):
            if s.timestamp - last[0] >= 1.0:
                last[0] = s.timestamp
                print(_fmt(s), flush=True)

        neo.on_update = show
        neo.stroke.right_first = not args.swap_lr
        if args.stroke:
            neo.on_revolution = lambda r: print(_fmt_rev(r), flush=True)
        if args.raw:
            neo.on_raw = lambda src, b: print(f"  [{src}] {b.hex()}")
        stop = asyncio.Event()
        hrm_task = None
        if args.hrm or args.hrm_address:
            hrm = HeartRateMonitor(args.hrm_address)
            neo.attach_hrm(hrm)
            hrm_task = asyncio.create_task(hrm.run(stop))
        try:
            await _sleep(args.seconds)
        finally:
            stop.set()
            if hrm_task:
                hrm_task.cancel()
            print("\nSession summary:")
            for k, v in neo.stats.summary().items():
                print(f"{k:>22}: {v}")


def _check(args) -> None:
    import tacx_workout as tw
    try:
        user = tw.load_user(args.user)
        if user:
            age = f", age {user.age}" if user.age else ""
            print(f"User: {user.display_name} ({user.path})  FTP {user.ftp:.0f} W, "
                  f"{user.weight_kg:g} kg ({user.ftp / user.weight_kg:.2f} W/kg){age}")
        else:
            print("User: no user.json found")
        if args.workout:
            w = tw.load_workout(args.workout)
            ftp = args.ftp or (user.ftp if user else None)
            tw.WorkoutPlayer(w, ftp)  # runs the FTP / power-limit checks
            print(w.summary(ftp))
        print("OK")
    except tw.WorkoutError as e:
        raise SystemExit(f"Error: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Tacx NEO 2T Smart BLE tool")
    ap.add_argument("-a", "--address", help="BLE address (default: scan for 'Tacx Neo')")
    ap.add_argument("--control", choices=["auto", "ftms", "fec"], default="auto",
                    help="control protocol for setting load (default: FTMS if available, else FE-C)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list nearby trainers and heart rate straps")
    s.add_argument("--timeout", type=float, default=8.0)
    s.add_argument("--all", action="store_true", help="list every BLE device, not just trainers")
    sub.add_parser("info", help="read device information and capabilities")

    def ride(p):
        p.add_argument("-t", "--seconds", type=float, default=0, help="run time (0 = until Ctrl-C)")
        p.add_argument("--weight", type=float, help="rider weight kg (sent via FE-C page 55)")
        p.add_argument("--bike-weight", type=float, default=8.0)
        p.add_argument("--raw", action="store_true", help="dump raw notifications")
        p.add_argument("--ant", action="store_true",
                       help="also read ANT+ pedal stroke analysis (needs a USB ANT+ stick)")
        p.add_argument("--ant-device", type=int, default=0, help="ANT+ device number (0 = first found)")
        p.add_argument("--stroke", action="store_true",
                       help="print every crank revolution: L/R split, peak torque angles, torque profile")
        p.add_argument("--swap-lr", action="store_true",
                       help="swap which crank half is the left leg (check with one-legged pedalling)")
        hrm_args(p)
        return p

    def user_args(p):
        p.add_argument("--user", metavar="FILE", help="user file (default: user.json here or next to this script)")
        p.add_argument("--ftp", type=float, help="FTP in watts, overriding the user file for this session")

    def hrm_args(p):
        p.add_argument("--hrm", action="store_true", help="connect a Bluetooth heart rate strap (first found)")
        p.add_argument("--hrm-address", metavar="ADDR", help="heart rate strap BLE address (implies --hrm)")

    p = sub.add_parser("launcher", help="choose a rider and a workout, then ride or edit it")
    p.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)

    p = sub.add_parser("editor", help="visual workout editor")
    p.add_argument("file", nargs="?", help=".workout.json or .zwo to open (default: a new workout)")
    user_args(p)
    p.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)

    p = sub.add_parser("check", help="validate the user file and a workout, and list its steps")
    p.add_argument("workout", nargs="?", help=".workout.json or .zwo file")
    user_args(p)

    p = sub.add_parser("hrm", help="heart rate strap only (no trainer)")
    p.add_argument("-t", "--seconds", type=float, default=0, help="run time (0 = until Ctrl-C)")
    p.add_argument("--hrm-address", metavar="ADDR", help="strap BLE address (default: first found)")

    p = sub.add_parser("ant", help="ANT+ pedal stroke analysis only (no Bluetooth)")
    p.add_argument("-t", "--seconds", type=float, default=0, help="run time (0 = until Ctrl-C)")
    p.add_argument("--ant-device", type=int, default=0, help="ANT+ device number (0 = first found)")
    p.add_argument("--raw", action="store_true", help="dump raw ANT+ pages")

    ride(sub.add_parser("monitor", help="stream live data"))
    ride(sub.add_parser("erg", help="ERG mode at fixed power")).add_argument("watts", type=int)
    ride(sub.add_parser("resistance", help="fixed resistance 0-100%%")).add_argument("percent", type=float)
    p = ride(sub.add_parser("sim", help="simulation / slope mode"))
    p.add_argument("grade", type=float, help="gradient %%")
    p.add_argument("--wind", type=float, default=0.0, help="head wind m/s")
    p.add_argument("--crr", type=float, default=0.004)
    p.add_argument("--cw", type=float, default=0.51)

    p = sub.add_parser("dashboard", help="graphical dashboard: power, cadence, speed, pedal balance peanut")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--erg", type=int, metavar="W", help="ERG mode at this power")
    mode.add_argument("--resistance", type=float, metavar="PCT", help="fixed resistance 0-100%%")
    mode.add_argument("--grade", type=float, metavar="PCT", help="slope mode at this gradient")
    mode.add_argument("--workout", metavar="FILE", help="ride a structured workout (.workout.json or .zwo)")
    p.add_argument("--weight", type=float, help="rider weight kg (default: from the user file)")
    p.add_argument("--bike-weight", type=float, help="bike weight kg (default: user file, else 8)")
    p.add_argument("--swap-lr", action="store_true", help="swap which crank half is the left leg")
    hrm_args(p)
    user_args(p)
    p.add_argument("--no-autopause", action="store_true",
                   help="keep the workout clock running when you stop pedalling")
    p.add_argument("--replay", metavar="PCAPNG", help="play back a Bluetooth capture instead of connecting")
    p.add_argument("--replay-speed", type=float, default=1.0, help="replay speed multiplier")
    p.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)
    p.add_argument("-t", "--seconds", type=float, default=0, help=argparse.SUPPRESS)

    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.cmd == "dashboard":  # Qt owns the main thread; Bluetooth runs in a worker
        from tacx_dashboard import run
        run(args)
        return
    if args.cmd == "launcher":
        from tacx_launcher import run as run_launcher
        run_launcher(args)
        return
    if args.cmd == "editor":
        from tacx_editor import run as run_editor
        run_editor(args)
        return
    if args.cmd == "check":
        _check(args)
        return
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
