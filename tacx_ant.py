"""
tacx_ant.py - ANT+ pedal stroke analysis (Cycling Dynamics) from a Tacx NEO 2T.

The NEO 2T broadcasts an ANT+ Bicycle Power channel (device type 11). Besides
power/cadence it can send the Cycling Dynamics pages defined in the ANT+
Bicycle Power Device Profile Rev 5.1:

  0x10  Power-only            power, cadence, L/R pedal power %
  0x13  TE / PS               torque effectiveness + pedal smoothness, per leg
  0xE0  Right force angle     power phase start/end, peak phase, avg torque
  0xE1  Left force angle      "
  0xE2  Pedal position        platform centre offset (PCO) per leg, seated/standing
  0x14  Torque barycenter     angle of the torque "centre of mass"

Pages 0xE0/E1/E2/14 are only sent once a display enables them: request the
Advanced Capabilities 2 subpage (0x02/0xFE), set 8 Hz + the dynamics bits, and
switch the receiving channel period to 8 Hz. This module does that for you.

Needs a USB ANT+ stick (e.g. Garmin ANT+ USB-m) and:  pip install openant
On Windows the stick needs the WinUSB driver (install it with Zadig).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from openant.easy.node import Node
    from openant.easy.channel import Channel
except ImportError:  # parsers remain usable without openant (e.g. for tests)
    Node = Channel = None

log = logging.getLogger("tacx_ant")

ANTPLUS_NETWORK_KEY = [0xB9, 0xA5, 0x21, 0xFB, 0xBD, 0x72, 0xC3, 0x45]
BP_DEVICE_TYPE = 11
BP_RF_FREQ = 57          # 2457 MHz
BP_PERIOD_4HZ = 8182
BP_PERIOD_8HZ = 4091

RIDER_POSITION = {0: "seated", 1: "->seated", 2: "standing", 3: "->standing"}

# Advanced Capabilities 2 bits (byte 4 mask / byte 6 value). 0 = supported/enabled.
CAP_4HZ, CAP_8HZ, CAP_PHASE, CAP_PCO, CAP_POSITION, CAP_BARYCENTER = 0x01, 0x02, 0x08, 0x10, 0x20, 0x40


def _brads_to_deg(b: int) -> float:
    return round(b * 360 / 256, 1)


# --- Parsers (pure functions) -----------------------------------------------
def parse_bp_page(p: bytes) -> dict:
    """Decode one 8-byte ANT+ Bicycle Power data page."""
    page = p[0]
    out = {"page": page}
    if page == 0x10:  # Standard power-only
        out["cadence_rpm"] = None if p[3] == 0xFF else p[3]
        out["power_w"] = p[6] | (p[7] << 8)
        if p[2] != 0xFF:
            pct = p[2] & 0x7F
            if p[2] & 0x80:  # value is the right pedal's contribution
                out["right_pct"], out["left_pct"] = pct, 100 - pct
            else:
                out["unknown_side_pct"] = pct
    elif page == 0x13:  # Torque effectiveness & pedal smoothness
        half = lambda v: None if v == 0xFF else v / 2
        out["left_te_pct"] = half(p[2])
        out["right_te_pct"] = half(p[3])
        if p[5] == 0xFE:
            out["combined_ps_pct"] = half(p[4])
        else:
            out["left_ps_pct"] = half(p[4])
            out["right_ps_pct"] = half(p[5])
    elif page in (0xE0, 0xE1):  # Right / left force angle (power phase)
        side = "right" if page == 0xE0 else "left"
        if not (p[2] == 0xC0 and p[3] == 0xC0):
            out[f"{side}_phase_start_deg"] = _brads_to_deg(p[2])
            out[f"{side}_phase_end_deg"] = _brads_to_deg(p[3])
        if not (p[4] == 0xC0 and p[5] == 0xC0):
            out[f"{side}_peak_start_deg"] = _brads_to_deg(p[4])
            out[f"{side}_peak_end_deg"] = _brads_to_deg(p[5])
        out[f"{side}_torque_nm"] = (p[6] | (p[7] << 8)) / 32
    elif page == 0xE2:  # Pedal position data
        out["rider_position"] = RIDER_POSITION[p[2] >> 6]
        if p[3] != 0xFF:
            out["cadence_rpm"] = p[3]
        sb = lambda v: v - 256 if v > 127 else v
        if p[4] != 0x80:
            out["right_pco_mm"] = sb(p[4])
        if p[5] != 0x80:
            out["left_pco_mm"] = sb(p[5])
    elif page == 0x14:  # Torque barycenter
        out["barycenter_deg"] = p[1] * 0.5 + 30
    elif page == 0x02 and p[1] == 0xFE:  # Advanced capabilities 2
        out["cap_supported"] = (~p[4]) & 0x7B  # 0 bit = supported
        out["cap_enabled"] = (~p[6]) & 0x7B    # 0 bit = enabled
    elif page == 0x50:
        out["manufacturer_id"] = p[4] | (p[5] << 8)
        out["model_number"] = p[6] | (p[7] << 8)
    elif page == 0x51:
        out["sw_revision"] = p[3]
        out["serial_number"] = int.from_bytes(p[4:8], "little")
    else:
        out["raw"] = bytes(p).hex()
    return out


def caps_str(bits: int) -> str:
    names = [(CAP_8HZ, "8Hz"), (CAP_PHASE, "power-phase"), (CAP_PCO, "PCO"),
             (CAP_POSITION, "rider-position"), (CAP_BARYCENTER, "barycenter")]
    return ", ".join(n for b, n in names if bits & b) or "none"


def set_caps_page(enable: int) -> list[int]:
    """Get/Set page 0x02 subpage 0xFE: switch to 8 Hz and enable `enable` bits."""
    # mask: 0 = apply this bit, 1 = ignore; reserved bits 2 and 7 always 1
    mask = 0x84 | ((CAP_PHASE | CAP_PCO | CAP_POSITION | CAP_BARYCENTER) & ~enable)
    value = 0x84 | CAP_4HZ  # stop 4 Hz, start 8 Hz (0 = transmit/enable)
    value |= (CAP_PHASE | CAP_PCO | CAP_POSITION | CAP_BARYCENTER) & ~enable
    return [0x02, 0xFE, 0xFF, 0xFF, mask, 0xFF, value, 0xFF]


def request_page(page: int, subpage: int = 0xFF, times: int = 4) -> list[int]:
    """Common page 70 (0x46) Request Data Page."""
    return [0x46, 0xFF, 0xFF, subpage, 0xFF, times & 0x7F, page, 0x01]


# --- State and stats --------------------------------------------------------
@dataclass
class PedalStroke:
    timestamp: float = 0.0
    power_w: Optional[int] = None
    cadence_rpm: Optional[int] = None
    left_pct: Optional[float] = None
    right_pct: Optional[float] = None
    left_te_pct: Optional[float] = None
    right_te_pct: Optional[float] = None
    left_ps_pct: Optional[float] = None
    right_ps_pct: Optional[float] = None
    combined_ps_pct: Optional[float] = None
    left_phase_start_deg: Optional[float] = None
    left_phase_end_deg: Optional[float] = None
    left_peak_start_deg: Optional[float] = None
    left_peak_end_deg: Optional[float] = None
    left_torque_nm: Optional[float] = None
    right_phase_start_deg: Optional[float] = None
    right_phase_end_deg: Optional[float] = None
    right_peak_start_deg: Optional[float] = None
    right_peak_end_deg: Optional[float] = None
    right_torque_nm: Optional[float] = None
    left_pco_mm: Optional[int] = None
    right_pco_mm: Optional[int] = None
    rider_position: Optional[str] = None
    barycenter_deg: Optional[float] = None

    @property
    def left_power_w(self) -> Optional[float]:
        if self.power_w is None or self.left_pct is None:
            return None
        return round(self.power_w * self.left_pct / 100, 1)

    @property
    def right_power_w(self) -> Optional[float]:
        lp = self.left_power_w
        return None if lp is None else round(self.power_w - lp, 1)


class _Avg:
    def __init__(self):
        self.n, self.s = 0, 0.0

    def add(self, v):
        if v is not None:
            self.n += 1
            self.s += v

    @property
    def value(self):
        return round(self.s / self.n, 1) if self.n else None


@dataclass
class DynamicsStats:
    started: float = field(default_factory=time.time)
    avgs: dict = field(default_factory=dict)
    position_time: dict = field(default_factory=lambda: {"seated": 0.0, "standing": 0.0})
    _last_pos: Optional[tuple] = None

    def add(self, fields: dict, now: float) -> None:
        for k, v in fields.items():
            if k.endswith(("_pct", "_deg", "_mm", "_nm")) and isinstance(v, (int, float)):
                self.avgs.setdefault(k, _Avg()).add(v)
        if "power_w" in fields and "left_pct" in fields:
            lp = fields["power_w"] * fields["left_pct"] / 100
            self.avgs.setdefault("left_power_w", _Avg()).add(lp)
            self.avgs.setdefault("right_power_w", _Avg()).add(fields["power_w"] - lp)
        if "rider_position" in fields:
            if self._last_pos:
                pos, t = self._last_pos
                # transitions are attributed to the state being transitioned to
                key = "standing" if "standing" in pos else "seated"
                self.position_time[key] += min(now - t, 5.0)
            self._last_pos = (fields["rider_position"], now)

    def summary(self) -> dict:
        out = {k: a.value for k, a in sorted(self.avgs.items())}
        out.update({f"time_{k}_s": round(v, 1) for k, v in self.position_time.items()})
        return out


# --- Receiver ---------------------------------------------------------------
class TacxNeoAnt:
    """
    Background ANT+ receiver for the NEO's bicycle power channel.

        ant = TacxNeoAnt()                 # device_number=0 pairs with the first one found
        ant.on_update = lambda s: print(s.left_te_pct, s.right_te_pct)
        ant.start()
        ...
        ant.stop(); print(ant.stats.summary())
    """

    def __init__(self, device_number: int = 0, enable_dynamics: bool = True):
        if Node is None:
            raise RuntimeError("openant is not installed: pip install openant")
        self.device_number = device_number
        self.enable_dynamics = enable_dynamics
        self.state = PedalStroke()
        self.stats = DynamicsStats()
        self.info: dict = {}
        self.on_update: Optional[Callable[[PedalStroke], None]] = None
        self.on_raw: Optional[Callable[[bytes], None]] = None
        self._node = None
        self._channel = None
        self._thread = None
        self._paired = False
        self._caps_seen = threading.Event()
        self._dyn_enabled = False

    def start(self) -> None:
        self._node = Node()
        self._node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        ch = self._node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE, 0x00, 0x01)
        ch.on_broadcast_data = self._on_data
        ch.on_burst_data = self._on_data
        ch.set_id(self.device_number, BP_DEVICE_TYPE, 0)
        ch.enable_extended_messages(1)
        ch.set_search_timeout(0xFF)
        ch.set_period(BP_PERIOD_4HZ)
        ch.set_rf_freq(BP_RF_FREQ)
        ch.open()
        self._channel = ch
        self._thread = threading.Thread(target=self._node.start, name="ant-node", daemon=True)
        self._thread.start()
        log.info("ANT+ searching for bicycle power device #%s", self.device_number or "any")

    def stop(self) -> None:
        if self._node:
            try:
                self._channel.close()
            except Exception:
                pass
            self._node.stop()
            self._node = None

    # ----- configuration -----
    def _configure(self) -> None:
        """Runs in its own thread: query capabilities, enable cycling dynamics, go 8 Hz."""
        try:
            for _ in range(3):
                self._channel.send_acknowledged_data(request_page(0x02, 0xFE))
                if self._caps_seen.wait(2.0):
                    break
            sup = self.info.get("cap_supported")
            if sup is None:
                log.warning("No Advanced Capabilities reply - trying to enable cycling dynamics anyway")
                sup = CAP_8HZ | CAP_PHASE | CAP_PCO | CAP_POSITION | CAP_BARYCENTER
            else:
                print(f"ANT+ cycling dynamics supported: {caps_str(sup)}; "
                      f"enabled: {caps_str(self.info.get('cap_enabled', 0))}", flush=True)
            if not sup & CAP_8HZ:
                log.warning("Sensor does not support 8 Hz - cycling dynamics pages unavailable")
                return
            want = sup & (CAP_PHASE | CAP_PCO | CAP_POSITION | CAP_BARYCENTER)
            self._channel.send_acknowledged_data(set_caps_page(want))
            self._channel.set_period(BP_PERIOD_8HZ)
            self._dyn_enabled = True
            log.info("Requested 8 Hz + %s", caps_str(want))
        except Exception as e:
            log.error("ANT+ configuration failed: %s", e)

    # ----- data -----
    def _on_data(self, data) -> None:
        data = bytes(data)
        if len(data) > 12 and not self._paired:
            self._paired = True
            dev = data[9] | (data[10] << 8)
            self.info["device_number"] = dev
            print(f"ANT+ paired with bicycle power device #{dev}", flush=True)
            if self.enable_dynamics:
                threading.Thread(target=self._configure, daemon=True).start()
        page = data[:8]
        if self.on_raw:
            self.on_raw(page)
        f = parse_bp_page(page)
        if f["page"] in (0x02, 0x50, 0x51):
            self.info.update({k: v for k, v in f.items() if k != "page"})
            if f["page"] == 0x02:
                self._caps_seen.set()
            return
        now = time.time()
        self.state.timestamp = now
        for k, v in f.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        self.stats.add(f, now)
        if self.on_update:
            self.on_update(self.state)


def format_stroke(s: PedalStroke) -> str:
    f = lambda v, fmt="{:.0f}": "-" if v is None else fmt.format(v)
    phase = lambda a, b: "-" if a is None else f"{a:.0f}-{b:.0f}"
    ps = (f"PS {f(s.combined_ps_pct)}%" if s.combined_ps_pct is not None
          else f"PS {f(s.left_ps_pct)}/{f(s.right_ps_pct)}%")
    return (f"  ANT  P {f(s.power_w):>4} W  L/R {f(s.left_power_w):>4}/{f(s.right_power_w):<4} W  "
            f"TE {f(s.left_te_pct)}/{f(s.right_te_pct)}%  {ps}  "
            f"phase L {phase(s.left_phase_start_deg, s.left_phase_end_deg)} "
            f"R {phase(s.right_phase_start_deg, s.right_phase_end_deg)}  "
            f"peak L {phase(s.left_peak_start_deg, s.left_peak_end_deg)} "
            f"R {phase(s.right_peak_start_deg, s.right_peak_end_deg)}  "
            f"PCO {f(s.left_pco_mm)}/{f(s.right_pco_mm)} mm  "
            f"bary {f(s.barycenter_deg, '{:.1f}')}  {s.rider_position or '-'}")
