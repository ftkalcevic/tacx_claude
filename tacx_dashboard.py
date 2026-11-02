"""
tacx_dashboard.py - live graphical dashboard for the Tacx NEO 2T.

Shows power, cadence, virtual speed, left/right balance, 2-minute history
charts and the "pedal balance peanut": a polar plot of torque around the crank,
right leg 0-180 deg (right of the plot), left leg 180-360 deg (left), TDC at top.

Qt (PySide6) runs the window on the main thread; the Bluetooth session runs
its own asyncio loop in a worker thread and the GUI polls it on a timer.

    python tacx_neo.py dashboard
    python tacx_neo.py dashboard --erg 150
    python tacx_neo.py dashboard --hrm                            # with a heart rate strap
    python tacx_neo.py dashboard --workout workouts\\vo2_5x3.workout.json
    python tacx_neo.py dashboard --replay C:\\BTP\\neo.pcapng      # no trainer needed
"""

from __future__ import annotations

import asyncio
import collections
import json
import math
import os
import struct
import threading
import time
from typing import Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

import tacx_neo as tn
import tacx_workout as tw
from tacx_sound import CountdownBeeper

HISTORY_S = 120          # length of the history charts
PEANUT_REVS = 5          # revolutions averaged into the peanut
PEANUT_BIN_DEG = 10      # angular resolution of the averaged peanut
STALE_S = 3.0            # values older than this show as "--"
MESSAGE_S = 10.0         # how long a workout text message stays on screen

COL_POWER = "#ff9f1c"
COL_CADENCE = "#2ec4b6"
COL_SPEED = "#8ac926"
COL_HR = "#e63946"
COL_LEFT = "#4ea1ff"
COL_RIGHT = "#ff5d73"
COL_GRID = "#3a3f47"
COL_TEXT = "#c9d1d9"
COL_BG = "#0d1117"
COL_PANEL = "#161b22"


# --- Data sources -------------------------------------------------------------
class Shared:
    """Latest data, written by a worker thread and read by the GUI timer."""

    def __init__(self):
        self.state: Optional[tn.TrainerState] = None
        self.revs: collections.deque = collections.deque(maxlen=50)
        self.rev_count = 0
        self.status = "Connecting..."
        self.hrm: Optional[tn.HeartRateMonitor] = None  # set when a strap is in use
        self.user: Optional[tw.UserProfile] = None
        self.player: Optional[tw.WorkoutPlayer] = None  # set when riding a workout
        self.stats: Optional[tn.SessionStats] = None    # the ride's running totals (energy etc.)

    def add_rev(self, rev: tn.Revolution) -> None:
        self.revs.append(rev)
        self.rev_count += 1


def _weights(args, user: Optional[tw.UserProfile]) -> tuple:
    """Rider and bike weight: command line first, then the user file."""
    weight = args.weight or (user.weight_kg if user else None)
    bike = args.bike_weight or (user.bike_weight_kg if user else 8.0)
    return weight, bike


class TrainerWorker(threading.Thread):
    """Runs a TacxNeo Bluetooth session until stop() is called."""

    def __init__(self, shared: Shared, args):
        super().__init__(daemon=True, name="tacx-ble")
        self.shared, self.args = shared, args
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        a = self.args
        try:
            self.shared.status = "Scanning for trainer..."
            async with tn.TacxNeo(a.address, control=a.control) as neo:
                neo.stroke.right_first = not a.swap_lr
                neo.on_update = lambda s: setattr(self.shared, "state", s)
                neo.on_revolution = self.shared.add_rev
                self.shared.stats = neo.stats
                weight, bike = _weights(a, self.shared.user)
                if weight:
                    await neo.set_user_config(weight, bike)
                mode = "free ride"
                player_task = None
                if self.shared.player:
                    player_task = asyncio.create_task(tw.drive(self.shared.player, neo, self._stop))
                    mode = f"workout: {self.shared.player.workout.name}"
                elif a.erg is not None:
                    await neo.set_target_power(a.erg)
                    mode = f"ERG {a.erg} W"
                elif a.resistance is not None:
                    await neo.set_resistance(a.resistance)
                    mode = f"resistance {a.resistance:g}%"
                elif a.grade is not None:
                    await neo.set_simulation(a.grade)
                    mode = f"slope {a.grade:g}%"
                self.shared.status = f"Connected to {neo.address}  |  {neo.control.upper()} control  |  {mode}"
                hrm_task = None
                if a.hrm or a.hrm_address:  # started after the trainer so the two scans don't overlap
                    hrm = tn.HeartRateMonitor(a.hrm_address)
                    neo.attach_hrm(hrm)
                    self.shared.hrm = hrm
                    hrm_task = asyncio.create_task(hrm.run(self._stop))
                await self._stop.wait()
                for task in (hrm_task, player_task):
                    if task:
                        task.cancel()
            self.shared.status = "Disconnected"
        except Exception as e:
            self.shared.status = f"Error: {e}"

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)


def _pcapng_att(path: str):
    """Yield (timestamp, connection handle, ATT PDU) from a BTVS / Wireshark HCI capture."""
    d = open(path, "rb").read()
    o = 0
    while o + 8 <= len(d):
        btype, blen = struct.unpack_from("<II", d, o)
        if blen < 12:
            break
        if btype == 6:  # Enhanced Packet Block
            _, th, tl, cap = struct.unpack_from("<IIII", d, o + 8)
            p = d[o + 28:o + 28 + cap][4:]  # skip the HCI pseudo-header (direction)
            if len(p) > 9 and p[0] == 0x02:  # ACL data
                h = struct.unpack_from("<H", p, 1)[0]
                l2len, cid = struct.unpack_from("<HH", p, 5)
                if (h >> 12) & 0x3 == 2 and cid == 4:  # first fragment, ATT channel
                    yield ((th << 32) | tl) * 1e-6, h & 0x0FFF, p[9:9 + l2len]
        o += blen


class ReplayWorker(threading.Thread):
    """Plays back a Bluetooth capture of the Tacx app through TacxNeo's decoders."""

    # ATT value handles on the NEO 2T (firmware 2.7.0), from the GATT discovery in a capture
    H_CPS_MEASUREMENT, H_VECTOR, H_FEC_RX = 0x001B, 0x0022, 0x0029

    def __init__(self, shared: Shared, args):
        super().__init__(daemon=True, name="tacx-replay")
        self.shared, self.args = shared, args
        self._stop = threading.Event()

    def run(self) -> None:
        events = []
        for ts, conn, a in _pcapng_att(self.args.replay):
            if a and a[0] == 0x1B and len(a) >= 3:
                events.append((ts, conn, struct.unpack_from("<H", a, 1)[0], bytes(a[3:])))
        conns = collections.Counter(c for _, c, h, _ in events if h == self.H_VECTOR)
        if not conns:
            self.shared.status = "Replay: no Cycling Power Vector data in capture"
            return
        conn = conns.most_common(1)[0][0]
        events = [e for e in events if e[1] == conn]
        speed = self.args.replay_speed
        if self.shared.player:  # run the workout clock too (no trainer to send targets to)
            threading.Thread(target=self._tick_player, daemon=True, name="tacx-replay-player").start()
        while not self._stop.is_set():
            neo = tn.TacxNeo()
            neo._has = lambda uuid: False
            neo.stroke.right_first = not self.args.swap_lr
            neo.on_update = lambda s: setattr(self.shared, "state", s)
            neo.on_revolution = self.shared.add_rev
            self.shared.stats = neo.stats
            handlers = {self.H_CPS_MEASUREMENT: neo._on_cps, self.H_VECTOR: neo._on_vector,
                        self.H_FEC_RX: neo._on_fec}
            self.shared.status = f"Replaying {self.args.replay} at {speed:g}x"
            t0, w0 = events[0][0], time.time()
            for ts, _, handle, value in events:
                delay = (ts - t0) / speed - (time.time() - w0)
                if delay > 0 and self._stop.wait(delay):
                    return
                fn = handlers.get(handle)
                if fn:
                    fn(None, bytearray(value))

    def _tick_player(self) -> None:
        while not self._stop.wait(0.1):
            self.shared.player.tick(time.time(), *tw.rider_inputs(self.shared.state))

    def stop(self) -> None:
        self._stop.set()


# --- Widgets -----------------------------------------------------------------
class Tile(QtWidgets.QFrame):
    """A big number with a caption and unit. The dashboard sizes it once, from the widest
    text it's expected to show (widest()), so it never changes size during a ride."""

    def __init__(self, caption: str, unit: str, color: str, widest_value: str = "888",
                 widest_sub: str = ""):
        super().__init__()
        self.setObjectName("tile")
        self.widest_value, self.widest_sub = widest_value, widest_sub
        # the dashboard sets the width; the text inside never resizes the tile
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(0)
        self.caption = QtWidgets.QLabel(caption.upper())
        self.caption.setStyleSheet(f"color:{COL_TEXT}; font-size:11pt; letter-spacing:2px;")
        row = QtWidgets.QHBoxLayout()
        self.value = QtWidgets.QLabel("--")
        self.value.setStyleSheet(f"color:{color}; font-size:44pt; font-weight:600;")
        self.unit = QtWidgets.QLabel(unit)
        self.unit.setStyleSheet(f"color:{COL_TEXT}; font-size:14pt;")
        self.unit.setAlignment(QtCore.Qt.AlignBottom)
        row.addWidget(self.value)
        row.addWidget(self.unit)
        row.addStretch()
        self.sub = QtWidgets.QLabel(" ")  # a space, not "": keeps its line's height
        self.sub.setStyleSheet(f"color:{COL_TEXT}; font-size:10pt;")
        lay.addWidget(self.caption)
        lay.addLayout(row)
        lay.addWidget(self.sub)

    def widest(self) -> int:
        """Width this tile needs to show its widest expected value and second line."""
        return _measure(self, lambda: self.set(self.widest_value, self.widest_sub),
                        (self.value, self.sub))

    def set(self, text: str, sub: str = "") -> None:
        self.value.setText(text)
        self.sub.setText(sub or " ")


class BalanceTile(QtWidgets.QFrame):
    """Left/right percentages with a split bar."""

    def __init__(self):
        super().__init__()
        self.setObjectName("tile")
        # the dashboard sets the width; the text inside never resizes the tile
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        cap = QtWidgets.QLabel("PEDAL BALANCE")
        cap.setStyleSheet(f"color:{COL_TEXT}; font-size:11pt; letter-spacing:2px;")
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(16)  # keeps the two percentages apart at the measured width
        self.left = QtWidgets.QLabel("--")
        self.left.setStyleSheet(f"color:{COL_LEFT}; font-size:30pt; font-weight:600;")
        self.right = QtWidgets.QLabel("--")
        self.right.setStyleSheet(f"color:{COL_RIGHT}; font-size:30pt; font-weight:600;")
        self.right.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        row.addWidget(self.left)
        row.addStretch()
        row.addWidget(self.right)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(12)
        self.bar.setStyleSheet(
            f"QProgressBar {{ background:{COL_RIGHT}; border:none; border-radius:6px; }}"
            f"QProgressBar::chunk {{ background:{COL_LEFT}; border-radius:6px; }}")
        self.watts = QtWidgets.QLabel(" ")
        self.watts.setStyleSheet(f"color:{COL_TEXT}; font-size:10pt;")
        lay.addWidget(cap)
        lay.addLayout(row)
        lay.addWidget(self.bar)
        lay.addWidget(self.watts)

    def widest(self) -> int:
        """Width needed for the widest percentages and watts line. The two sides always add
        up to 100, so the widest pair is "L 100%" with "0% R" (every digit is the same width)."""
        def fill():
            self.left.setText("L 100%")
            self.right.setText("0% R")
            self.watts.setText("1888 W  /  1888 W")
        return _measure(self, fill, (self.left, self.right, self.watts))

    def set(self, left_pct: Optional[float], lw: Optional[float], rw: Optional[float]) -> None:
        if left_pct is None:
            self.left.setText("L --")
            self.right.setText("-- R")
            self.bar.setValue(500)
            self.watts.setText(" ")
            return
        self.left.setText(f"L {left_pct:.0f}%")
        self.right.setText(f"{100 - left_pct:.0f}% R")
        self.bar.setValue(int(left_pct * 10))
        self.watts.setText(" " if lw is None else f"{lw:.0f} W  /  {rw:.0f} W")


def _measure(tile: QtWidgets.QWidget, fill, labels) -> int:
    """Width the tile's layout asks for with fill() applied; the labels' text is put back."""
    saved = [lbl.text() for lbl in labels]
    for w in tile.findChildren(QtWidgets.QLabel):
        w.ensurePolished()  # stylesheet font sizes in place before measuring
    fill()
    width = tile.layout().sizeHint().width()
    for lbl, text in zip(labels, saved):
        lbl.setText(text)
    return width


class Peanut(pg.PlotWidget):
    """Polar torque plot of the pedal stroke: the pedal balance 'peanut'."""

    def __init__(self):
        super().__init__()
        self.setBackground(COL_PANEL)
        self.setAspectLocked(True)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setMouseEnabled(False, False)
        self.setMenuEnabled(False)
        self.hideButtons()
        self._grid_items: list = []
        self._scale = 0

        def path_item(color):
            item = QtWidgets.QGraphicsPathItem()
            c = QtGui.QColor(color)
            pen = QtGui.QPen(c, 2.5)
            pen.setCosmetic(True)
            item.setPen(pen)
            c.setAlpha(90)
            item.setBrush(QtGui.QBrush(c))
            self.addItem(item)
            return item

        self.right_lobe = path_item(COL_RIGHT)
        self.left_lobe = path_item(COL_LEFT)
        self.latest = pg.PlotDataItem(pen=pg.mkPen((255, 255, 255, 110), width=1))
        self.addItem(self.latest)
        # Text is pinned to the corners of the plot in pixels (parented to the view box,
        # not placed on the chart's scale), so it can't drift into the rings and angle
        # labels when the panel gets short: LEFT top-left, RIGHT top-right, info bottom-left.
        vb = self.getPlotItem().vb
        self.left_txt = pg.TextItem("", color=COL_LEFT, anchor=(0, 0))
        self.right_txt = pg.TextItem("", color=COL_RIGHT, anchor=(1, 0))
        self.info_txt = pg.TextItem("", color=COL_TEXT, anchor=(0, 1))
        for t in (self.left_txt, self.right_txt, self.info_txt):
            t.setFont(QtGui.QFont("Segoe UI", 11))
            t.setParentItem(vb)
        vb.sigResized.connect(self._place_text)
        self._set_scale(10)

    def _place_text(self, *_) -> None:
        r = self.getPlotItem().vb.boundingRect()
        self.left_txt.setPos(r.left() + 4, r.top() + 2)
        self.right_txt.setPos(r.right() - 4, r.top() + 2)
        self.info_txt.setPos(r.left() + 4, r.bottom() - 2)

    @staticmethod
    def _xy(theta_deg, r):
        th = np.radians(theta_deg)
        return r * np.sin(th), r * np.cos(th)  # 0 deg at top (TDC), clockwise

    def _set_scale(self, R: float) -> None:
        if R == self._scale:
            return
        self._scale = R
        for it in self._grid_items:
            self.removeItem(it)
        self._grid_items = []
        pen = QtGui.QPen(QtGui.QColor(COL_GRID), 1, QtCore.Qt.DashLine)
        pen.setCosmetic(True)
        step = R / 5  # R is a multiple of 5, so rings land on whole Nm
        for k in range(1, 6):
            r = step * k
            ring = QtWidgets.QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            ring.setPen(pen)
            self.addItem(ring)
            lbl = pg.TextItem(f"{r:g} Nm", color=COL_GRID, anchor=(0, 1))
            lbl.setPos(*self._xy(45, r))
            self.addItem(lbl)
            self._grid_items += [ring, lbl]
        for ang, name, anchor in ((0, "TDC 0°", (0.5, 1)), (90, "90°", (0, 0.5)),
                                  (180, "BDC 180°", (0.5, 0)), (270, "270°", (1, 0.5))):
            x, y = self._xy(ang, R)
            line = pg.PlotDataItem([0, x], [0, y], pen=pg.mkPen(COL_GRID, width=1))
            lbl = pg.TextItem(name, color=COL_TEXT, anchor=anchor)
            lbl.setPos(*self._xy(ang, R * 1.04))
            self.addItem(line)
            self.addItem(lbl)
            self._grid_items += [line, lbl]
        m = R * 1.35
        self.setRange(xRange=(-m, m), yRange=(-m * 0.95, m * 0.95), padding=0)

    def _lobe_path(self, theta, r) -> QtGui.QPainterPath:
        xs, ys = self._xy(theta, r)
        path = QtGui.QPainterPath(QtCore.QPointF(0, 0))
        for x, y in zip(xs, ys):
            path.lineTo(float(x), float(y))
        path.closeSubpath()
        return path

    def update_revs(self, revs: list) -> None:
        if not revs:
            return
        angles = np.concatenate([r.angles_deg for r in revs])
        torques = np.clip(np.concatenate([r.torques_nm for r in revs]), 0, None)
        nb = 360 // PEANUT_BIN_DEG
        idx = (angles // PEANUT_BIN_DEG).astype(int) % nb
        sums = np.bincount(idx, torques, nb)
        counts = np.bincount(idx, minlength=nb)
        centres = (np.arange(nb) + 0.5) * PEANUT_BIN_DEG
        ok = counts > 0
        if ok.sum() < 3:
            return
        theta = np.arange(0, 361, 2.0)
        r = np.interp(theta, centres[ok], sums[ok] / counts[ok], period=360)

        peak = float(r.max())
        self._set_scale(max(5, math.ceil(peak * 1.1 / 5) * 5))
        right = theta <= 180
        left = theta >= 180
        self.right_lobe.setPath(self._lobe_path(theta[right], r[right]))
        self.left_lobe.setPath(self._lobe_path(theta[left], r[left]))

        last = revs[-1]
        lt = np.clip(np.asarray(last.torques_nm), 0, None)
        la = np.asarray(last.angles_deg)
        order = np.argsort(la)
        x, y = self._xy(np.append(la[order], la[order][0]), np.append(lt[order], lt[order][0]))
        self.latest.setData(x, y)

        lp = sum(rv.left_pct for rv in revs) / len(revs)
        self.left_txt.setText(f"LEFT {lp:.0f}%\npeak {last.left_peak_nm:.1f} Nm @ {last.left_peak_deg}°")
        self.right_txt.setText(f"RIGHT {100 - lp:.0f}%\npeak {last.right_peak_nm:.1f} Nm @ {last.right_peak_deg}°")
        # two short lines: one long line reaches the BDC label in a small panel
        self.info_txt.setText(f"{last.rpm:.0f} rpm  ·  last {len(revs)} revs\n"
                              f"avg torque {last.avg_torque_nm:.1f} Nm")

    def reset(self) -> None:
        # not clear(): PlotWidget binds clear() to PlotItem.clear on the instance
        empty = QtGui.QPainterPath()
        self.right_lobe.setPath(empty)
        self.left_lobe.setPath(empty)
        self.latest.setData([], [])
        self.left_txt.setText("")
        self.right_txt.setText("")
        self.info_txt.setText("waiting for pedalling...")


class History(pg.GraphicsLayoutWidget):
    """Power, cadence, speed and heart rate over the last HISTORY_S seconds."""

    SERIES = (("power", "Power", "W", COL_POWER),
              ("cadence", "Cadence", "rpm", COL_CADENCE),
              ("speed", "Speed", "km/h", COL_SPEED),
              ("hr", "Heart rate", "bpm", COL_HR))

    def __init__(self):
        super().__init__()
        self.setBackground(COL_PANEL)
        self.curves = {}
        prev = None
        last = len(self.SERIES) - 1
        for i, (key, name, unit, color) in enumerate(self.SERIES):
            p = self.addPlot(row=i, col=0)
            # a horizontal caption in the top-left corner instead of a rotated axis title:
            # rotated titles are taller than the plots when the window is short, and collide
            caption = pg.TextItem(f"{name} ({unit})", color=color, anchor=(0, 0), fill=(22, 27, 34, 190))
            caption.setFont(QtGui.QFont("Segoe UI", 9))
            caption.setParentItem(p.vb)
            caption.setPos(4, 2)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setXRange(-HISTORY_S, 0, padding=0)
            p.setMouseEnabled(False, False)
            p.setMenuEnabled(False)
            p.hideButtons()
            p.getAxis("left").setWidth(40)
            if prev is not None:
                p.setXLink(prev)
            if i < last:
                p.getAxis("bottom").setStyle(showValues=False)
            else:
                p.setLabel("bottom", "seconds")
            if key == "hr":  # no fill to zero: HR lives well above it, so let the axis zoom in
                self.curves[key] = p.plot(pen=pg.mkPen(color, width=2), connect="finite")
            else:
                fill = QtGui.QColor(color)
                fill.setAlpha(40)
                self.curves[key] = p.plot(pen=pg.mkPen(color, width=2), fillLevel=0, brush=fill)
                p.setLimits(yMin=0)
            p.enableAutoRange("y", True)
            prev = p
        self.data = collections.deque()

    def add(self, now: float, power, cadence, speed, hr) -> None:
        self.data.append((now, power or 0, cadence or 0, speed or 0, hr if hr else np.nan))
        while self.data and now - self.data[0][0] > HISTORY_S:
            self.data.popleft()
        if len(self.data) < 2:
            return
        a = np.asarray(self.data, dtype=float)
        x = a[:, 0] - now
        for col, (key, *_ignored) in enumerate(self.SERIES, start=1):
            if key == "hr" and np.isnan(a[:, col]).all():
                self.curves[key].setData([], [])
                self.curves[key].getViewBox().setYRange(60, 180)  # sensible empty axis
                continue
            if key == "hr":
                self.curves[key].getViewBox().enableAutoRange("y", True)  # undo the fixed empty range
            self.curves[key].setData(x, a[:, col])


ZONES = ((55, "#8b949e"), (76, "#4ea1ff"), (91, "#2ec4b6"), (106, "#ffd166"), (121, "#ff9f1c"),
         (1e9, "#ff5d73"))  # upper % of FTP -> colour (recovery, endurance, tempo, threshold, VO2, anaerobic)
COL_NON_ERG = "#6e7681"


def zone_colour(watts: Optional[float], ftp: Optional[float]) -> str:
    if watts is None or not ftp:
        return COL_NON_ERG
    pct = 100 * watts / ftp
    return next(c for top, c in ZONES if pct < top)


TRACES = {  # workout chart overlays: key -> (label, colour, trace column)
    "power": ("Power", "#ffffff", 1),
    "hr": ("Heart rate", COL_HR, 3),
    "cadence": ("Cadence", COL_CADENCE, 2),
}
SCALES = {"hr": ("bpm", 60, 180), "cadence": ("rpm", 40, 120)}  # own axes: unit, default range
PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "dashboard.json")


def load_prefs() -> dict:
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(prefs: dict) -> None:
    os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
    with open(PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)


class WorkoutProfile(pg.PlotWidget):
    """The whole workout as coloured blocks, with progress and the rider's actual power,
    heart rate and cadence on top (heart rate and cadence on a right-hand axis)."""

    def __init__(self, player: tw.WorkoutPlayer):
        super().__init__()
        self.player = player
        self.setBackground(COL_PANEL)
        self.setMouseEnabled(False, False)
        self.setMenuEnabled(False)
        self.hideButtons()
        self.showGrid(x=False, y=True, alpha=0.12)
        self.setLabel("bottom", "minutes")
        self.setLabel("left", "W")
        self.getAxis("left").setWidth(48)
        self._blocks: list = []
        self._drawn_ftp = -1.0
        self.done = pg.LinearRegionItem((0, 0), movable=False, brush=(0, 0, 0, 110), pen=pg.mkPen(None))
        self.done.setZValue(10)
        self.addItem(self.done)
        self.marker = pg.InfiniteLine(0, angle=90, pen=pg.mkPen("#ffffff", width=2))
        self.marker.setZValue(30)
        self.addItem(self.marker)

        # Heart rate and cadence each get their own view box and scale. Their axes sit in
        # adjacent layout columns so the two axis lines meet: bpm labels on the left of the
        # shared line (a "left" axis), rpm labels on the right of it (the "right" axis).
        pi = self.getPlotItem()
        rpm_axis = pi.getAxis("right")
        pi.layout.removeItem(rpm_axis)
        pi.layout.addItem(rpm_axis, 2, 3)   # row 2 is the plot row; column 2 takes bpm
        bpm_axis = pg.AxisItem("left")
        pi.layout.addItem(bpm_axis, 2, 2)
        self.axes = {"hr": bpm_axis, "cadence": rpm_axis}
        self.units = {}  # "bpm" / "rpm" written across the top of each scale
        self.boxes = {"power": pi.vb}
        for key, (unit, _lo, _hi) in SCALES.items():
            vb = pg.ViewBox(enableMouse=False)
            vb.setZValue(25)  # lines above the workout blocks
            pi.scene().addItem(vb)
            vb.setXLink(pi)
            colour = TRACES[key][1]
            ax = self.axes[key]
            ax.linkToView(vb)
            ax.setPen(pg.mkPen(colour))
            ax.setTextPen(pg.mkPen(colour))
            ax.setGrid(False)  # the grid belongs to the watts scale only
            # unit label above the scale (row 1, over its axis), not rotated along it;
            # bpm hugs the shared line from the left, rpm from the right
            col = 2 if key == "hr" else 3
            unit_lbl = pg.LabelItem(unit, color=colour, size="9pt", justify="right" if col == 2 else "left")
            pi.layout.addItem(unit_lbl, 1, col)
            self.units[key] = unit_lbl
            # a fixed width that fits both the label and 3-digit ticks: if the label made
            # one column wider than its axis, the two axis lines would no longer meet
            digits = QtGui.QFontMetrics(QtWidgets.QApplication.font()).horizontalAdvance("000") + 18
            ax.setWidth(int(max(digits, unit_lbl.preferredWidth() + 4)))
            self.boxes[key] = vb
        pi.vb.sigResized.connect(self._sync_boxes)
        self.curves = {}
        for key, (_label, colour, _col) in TRACES.items():
            c = QtGui.QColor(colour)
            c.setAlpha(200)
            curve = pg.PlotDataItem(pen=pg.mkPen(c, width=1.6), connect="finite")
            curve.setZValue(20)
            self.boxes[key].addItem(curve)
            self.curves[key] = curve
        self._data = []  # trace points copied from the player so far
        self.set_visible({"power": True, "hr": False, "cadence": False})

    def _sync_boxes(self) -> None:
        rect = self.getPlotItem().vb.sceneBoundingRect()
        for key in SCALES:
            self.boxes[key].setGeometry(rect)

    def set_visible(self, shown: dict) -> None:
        for key, curve in self.curves.items():
            curve.setVisible(bool(shown.get(key)))
        for key, axis in self.axes.items():
            axis.setVisible(bool(shown.get(key)))
            self.units[key].setVisible(bool(shown.get(key)))
        self._fit_scales()

    def _fit_scales(self) -> None:
        """Each of the HR and cadence scales covers its default range and its data (±5)."""
        a = np.asarray(self._data, dtype=float) if self._data else None
        for key, (_unit, lo, hi) in SCALES.items():
            if a is not None:
                col = TRACES[key][2]
                vals = a[:, col][np.isfinite(a[:, col])]
                if len(vals):
                    lo, hi = min(lo, vals.min() - 5), max(hi, vals.max() + 5)
            self.boxes[key].setYRange(max(0.0, lo), hi, padding=0)

    def _draw_blocks(self, ftp: Optional[float]) -> None:
        for b in self._blocks:
            self.removeItem(b)
        self._blocks = []
        p = self.player
        base = ftp or 200
        top = base
        for s, t0 in zip(p.workout.steps, p.starts):
            x = [t0 / 60, (t0 + s.duration) / 60]
            if s.is_erg:
                y = [s.watts_at(0, ftp), s.watts_at(s.duration, ftp)]
            else:  # slope / resistance / free ride: a neutral block at half FTP height
                y = [base * 0.5, base * 0.5]
            top = max(top, *y)
            c = QtGui.QColor(zone_colour(y[0] if s.is_erg else None, ftp))
            c.setAlpha(200)
            item = pg.PlotDataItem(x, y, pen=pg.mkPen(c, width=1), fillLevel=0, brush=c)
            self.addItem(item)
            self._blocks.append(item)
        total = p.workout.total_s / 60
        self.setXRange(0, total, padding=0.01)
        self.setYRange(0, top * 1.1, padding=0)
        self._drawn_ftp = ftp

    def update_view(self, snap: dict) -> None:
        if snap["ftp"] != self._drawn_ftp:
            self._draw_blocks(snap["ftp"])
        pos = snap["position"] / 60
        self.marker.setValue(pos)
        self.done.setRegion((0, pos))
        if snap["trace_len"] > len(self._data):  # new points: about once a second
            self._data += self.player.trace_since(len(self._data))
            a = np.asarray(self._data, dtype=float)  # None -> nan: gaps in the lines
            for key, (_l, _c, col) in TRACES.items():
                self.curves[key].setData(a[:, 0] / 60, a[:, col])
            self._fit_scales()


class WorkoutPanel(QtWidgets.QFrame):
    """Workout progress plus the FTP % override and the Next button."""

    def __init__(self, player: tw.WorkoutPlayer, user: Optional[tw.UserProfile]):
        super().__init__()
        self.player = player
        self.setObjectName("tile")
        # height follows the contents (font sizes differ between screens), never squeezing them
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 14, 10)
        self.profile = WorkoutProfile(player)
        # the text column sets the panel height; the chart just fills it
        self.profile.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Ignored)
        self.profile.setMinimumHeight(200)
        # which actual-ride lines to draw over the workout: remembered between rides
        self.prefs = load_prefs()
        shown = {"power": True, "hr": False, "cadence": False, **self.prefs.get("workout_traces", {})}
        toggles = QtWidgets.QHBoxLayout()
        toggles.addWidget(QtWidgets.QLabel("Show:"))
        for key, (label, colour, _col) in TRACES.items():
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(shown[key])
            cb.setStyleSheet(f"QCheckBox {{ color:{colour}; font-weight:600; }}")
            cb.toggled.connect(lambda on, k=key: self._toggle_trace(k, on))
            toggles.addWidget(cb)
        toggles.addStretch()
        self.shown = shown
        self.profile.set_visible(shown)
        chart_col = QtWidgets.QVBoxLayout()
        chart_col.addWidget(self.profile, 1)
        chart_col.addLayout(toggles)
        lay.addLayout(chart_col, 3)

        info = QtWidgets.QVBoxLayout()
        info.setSpacing(2)
        lay.addLayout(info, 2)

        def label(size, color=COL_TEXT, bold=False):
            l = QtWidgets.QLabel("")
            l.setStyleSheet(f"color:{color}; font-size:{size}pt;" + (" font-weight:600;" if bold else ""))
            return l

        rider = f"  ·  {user.display_name}" if user else ""
        self.title = label(11)
        self.title.setText(f"WORKOUT  ·  {player.workout.name.upper()}{rider}")
        self.step_name = label(18, bold=True)
        self.group = label(10)
        big = QtWidgets.QHBoxLayout()
        self.remaining = label(40, bold=True)
        self.target = label(40, bold=True)
        self.target.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.target_base = label(11)   # unadjusted watts, shown when FTP % isn't 100
        self.target_base.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignTop)
        target_col = QtWidgets.QVBoxLayout()
        target_col.setSpacing(0)
        target_col.addWidget(self.target)
        target_col.addWidget(self.target_base)
        big.addWidget(self.remaining)
        big.addStretch()
        big.addLayout(target_col)
        self.detail = label(11)        # cadence target / paused
        self.next = label(11)
        prog = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.setStyleSheet(
            f"QProgressBar {{ background:#21262d; border:none; border-radius:5px; }}"
            f"QProgressBar::chunk {{ background:{COL_POWER}; border-radius:5px; }}")
        self.total = label(11)
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.total)

        controls = QtWidgets.QHBoxLayout()
        ftp_lbl = label(12, bold=True)
        ftp_lbl.setText("FTP")
        self.spin = QtWidgets.QSpinBox()
        self.spin.setRange(*tw.INTENSITY_RANGE)
        self.spin.setValue(100)
        self.spin.setSuffix(" %")
        self.spin.setSingleStep(1)
        self.spin.setAccelerated(True)
        self.spin.setFixedWidth(130)
        self.spin.setStyleSheet(
            f"QSpinBox {{ font-size:20pt; font-weight:600; color:{COL_TEXT}; background:#0d1117;"
            f" border:1px solid #30363d; border-radius:6px; padding:2px 6px; }}"
            f"QSpinBox::up-button, QSpinBox::down-button {{ width:28px; }}")
        self.spin.valueChanged.connect(player.set_intensity)
        self.ftp_txt = label(10)
        self.next_btn = QtWidgets.QPushButton("Next  ▶")
        self.next_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.next_btn.setStyleSheet(
            f"QPushButton {{ font-size:14pt; font-weight:600; color:#0d1117; background:{COL_CADENCE};"
            f" border:none; border-radius:6px; padding:8px 22px; }}"
            f"QPushButton:hover {{ background:#5fe0d4; }}"
            f"QPushButton:disabled {{ background:#30363d; color:#8b949e; }}")
        self.next_btn.clicked.connect(player.next_step)
        controls.addWidget(ftp_lbl)
        controls.addWidget(self.spin)
        controls.addWidget(self.ftp_txt, 1)
        controls.addWidget(self.next_btn)
        if player.base_ftp is None:  # watts-only workout: nothing to scale
            self.spin.setEnabled(False)

        for w in (self.title, self.step_name, self.group):
            info.addWidget(w)
        info.addLayout(big)
        info.addWidget(self.detail)
        info.addWidget(self.next)
        info.addStretch()
        info.addLayout(prog)
        info.addSpacing(6)
        info.addLayout(controls)

    def _toggle_trace(self, key: str, on: bool) -> None:
        self.shown[key] = on
        self.profile.set_visible(self.shown)
        self.prefs["workout_traces"] = dict(self.shown)
        save_prefs(self.prefs)

    def update_view(self, snap: dict, cadence: Optional[float]) -> None:
        self.profile.update_view(snap)
        s, ftp = snap["step"], snap["ftp"]
        if snap["base_ftp"]:
            self.ftp_txt.setText(f"  base {snap['base_ftp']:.0f} W\n  adjusted {ftp:.0f} W")
        else:
            self.ftp_txt.setText("  absolute watts\n  (no FTP scaling)")
        self.progress.setValue(int(1000 * snap["position"] / snap["total"]) if snap["total"] else 0)
        self.total.setText(f"  {tw.fmt_time(snap['position'])} / {tw.fmt_time(snap['total'])}")

        if snap["finished"] or s is None:
            self.step_name.setText("Workout complete")
            self.group.setText("")
            self.remaining.setText("0:00")
            self.target.setText("")
            self.target_base.setText(" ")  # keep the line, so the layout doesn't jump
            self.detail.setText(f"Trainer set to a light resistance ({tw.WorkoutPlayer.FINISH_RESISTANCE_PCT}%).")
            self.next.setText("")
            self.next_btn.setEnabled(False)
            return

        self.step_name.setText(f"{s.name}   ({snap['index'] + 1}/{snap['count']})")
        self.group.setText(s.group)
        # counts down to the next change: the step's end, or a skip after Next
        ttc = snap["time_to_change"]
        self.remaining.setText(tw.fmt_time(math.ceil(ttc)))
        final = ttc <= tw.WorkoutPlayer.COUNTDOWN_S
        self.remaining.setStyleSheet(f"color:{'#ffd166' if final else COL_TEXT}; font-size:40pt; font-weight:600;")
        if snap["skip_in"] is not None:
            self.next_btn.setText(f"Next in {math.ceil(snap['skip_in'])}")
            self.next_btn.setEnabled(False)
        else:
            self.next_btn.setText("Next  ▶")
            self.next_btn.setEnabled(not final)  # Next is ignored once the step is about to end anyway
        target_w = snap["target_w"]
        if s.is_erg and target_w is not None:
            self.target.setText(f"{target_w:.0f} W")
            self.target.setStyleSheet(f"color:{zone_colour(target_w, ftp)}; font-size:40pt; font-weight:600;")
        else:
            self.target.setText(s.describe(ftp))
            self.target.setStyleSheet(f"color:{COL_TEXT}; font-size:26pt; font-weight:600;")
        # only % FTP targets are scaled by the FTP spinner; absolute watts never change
        if s.kind == "ftp_pct" and snap["intensity"] != 100 and snap["base_ftp"]:
            base_w = s.watts_at(snap["step_elapsed"], snap["base_ftp"])
            self.target_base.setText(f"{base_w:.0f} W at 100%")
        else:
            self.target_base.setText(" ")  # keep the line, so the layout doesn't jump

        if not snap["started"]:
            self.detail.setText("Start pedalling to begin")
            self.detail.setStyleSheet("color:#ffd166; font-size:11pt;")
        elif snap["paused"]:
            self.detail.setText("PAUSED  -  start pedalling to resume")
            self.detail.setStyleSheet("color:#ffd166; font-size:11pt; font-weight:600;")
        elif s.cadence:
            lo, hi = s.cadence
            ok = cadence is not None and lo <= cadence <= hi
            self.detail.setText(f"Cadence target {lo:.0f}-{hi:.0f} rpm" + ("" if ok else "  ←"))
            self.detail.setStyleSheet(f"color:{COL_CADENCE if ok else COL_RIGHT}; font-size:11pt;")
        else:
            self.detail.setText("")
        n = snap["next"]
        self.next.setText("" if n is None else
                          f"Next:  {n.name}  ·  {tw.fmt_time(n.duration)}  @  {n.describe(ftp)}")


class MessageOverlay(QtWidgets.QLabel):
    """A workout text message in large type, floating over the middle of the window.
    Fades in and out, and lets mouse clicks through to the controls underneath."""

    FADE_S = 0.4

    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setWordWrap(True)
        self.setStyleSheet("color:#ffffff; font-size:40pt; font-weight:700;"
                           " background:rgba(13, 17, 23, 215); border:3px solid #ffd166;"
                           " border-radius:18px; padding:28px 44px;")
        self.opacity = QtWidgets.QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity)
        self._text = None
        self.hide()

    def show_message(self, text: Optional[str], age: float) -> None:
        if not text or age >= MESSAGE_S:
            self.hide()
            self._text = None
            return
        if text != self._text:
            self._text = text
            self.setText(text)
            self.place()
        self.opacity.setOpacity(max(0.0, min(1.0, age / self.FADE_S, (MESSAGE_S - age) / self.FADE_S)))
        self.show()
        self.raise_()

    def place(self) -> None:
        """Centre over the parent, at most 80% of its width, wrapping long messages."""
        win = self.parentWidget()
        self.ensurePolished()  # so the stylesheet's 40 pt font is in fontMetrics()
        one_line = self.fontMetrics().horizontalAdvance(self.text()) + 2 * 44 + 2 * 3 + 8
        w = min(int(win.width() * 0.8), one_line)  # one line if it fits, else wrap at 80%
        self.setFixedWidth(w)
        self.setFixedHeight(self.heightForWidth(w))
        self.move((win.width() - w) // 2, (win.height() - self.height()) // 2)


class Dashboard(QtWidgets.QMainWindow):
    def __init__(self, shared: Shared, worker, beeps: bool = True):
        super().__init__()
        self.shared, self.worker = shared, worker
        self.setWindowTitle("Tacx NEO Dashboard")
        self.resize(1400, 1140 if shared.player else 860)

        # widest text each tile is expected to show: sets the tile widths once, at start-up
        self.power = Tile("Power", "W", COL_POWER, "888", "8888 kJ")
        self.cadence = Tile("Cadence", "rpm", COL_CADENCE, "188")
        self.speed = Tile("Virtual speed", "km/h", COL_SPEED, "88.8", "distance 88.88 km")
        self.hr = Tile("Heart rate", "bpm", COL_HR, "188", "HRM 200  ·  battery 100%")
        self.balance = BalanceTile()
        self.peanut = Peanut()
        self.history = History()
        self.workout = WorkoutPanel(shared.player, shared.user) if shared.player else None

        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(8)  # with the measured widths, keeps the row within a 1080 px window
        row = (self.power, self.cadence, self.speed, self.hr, self.balance)
        for t in row:
            t.ensurePolished()  # apply the stylesheet fonts before measuring
        # Widths are measured once from each tile's widest expected text, never from the live
        # values, so no tile changes size during a ride. The four number tiles share the
        # widest of their widths; pedal balance gets its own (wider) one. Spare window width
        # is shared in proportion (stretch = width), so a resized window keeps that look.
        num_w = max(t.widest() for t in row[:4])
        widths = {t: num_w for t in row[:4]}
        widths[self.balance] = max(self.balance.widest(), num_w + 1)
        tile_h = max(t.sizeHint().height() for t in row)
        for t in row:
            t.setFixedHeight(tile_h)  # one height for the whole row
            t.setMinimumWidth(widths[t])
            tiles.addWidget(t, widths[t])
        body = QtWidgets.QHBoxLayout()
        body.addWidget(self._panel("Pedal stroke", self.peanut), 5)
        body.addWidget(self._panel("Last 2 minutes", self.history), 6)
        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(12, 12, 12, 6)
        lay.setSpacing(12)
        lay.addLayout(tiles)
        if self.workout:
            lay.addWidget(self.workout)
        lay.addLayout(body, 1)
        self.setCentralWidget(central)
        self.statusBar().setStyleSheet(f"color:{COL_TEXT};")

        self._seen_revs = -1
        self._last_hist = 0.0
        self.peanut.reset()
        self.overlay = MessageOverlay(self)  # child of the window: drawn over everything
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)
        # countdown beeps run on their own thread, so redraws here can't break them up
        self.beeper = CountdownBeeper(shared.player) if beeps and shared.player else None
        if self.beeper:
            self.beeper.start()

    def _panel(self, title: str, widget: QtWidgets.QWidget) -> QtWidgets.QFrame:
        f = QtWidgets.QFrame()
        f.setObjectName("tile")
        lay = QtWidgets.QVBoxLayout(f)
        lay.setContentsMargins(10, 8, 10, 10)
        lbl = QtWidgets.QLabel(title.upper())
        lbl.setStyleSheet(f"color:{COL_TEXT}; font-size:11pt; letter-spacing:2px;")
        lay.addWidget(lbl)
        lay.addWidget(widget, 1)
        return f

    def refresh(self) -> None:
        sh = self.shared
        self.statusBar().showMessage(sh.status + (f"  |  {sh.hrm.status}" if sh.hrm else ""))
        s = sh.state
        now = time.time()
        live = s is not None and now - s.timestamp < STALE_S

        # heart rate: from the strap when one is in use, else whatever the trainer forwards
        if sh.hrm:
            hr = sh.hrm.heart_rate if sh.hrm.connected and now - sh.hrm.last_time < 5 else None
            hr_sub = "" if not sh.hrm.connected else (
                f"{sh.hrm.name}" + ("" if sh.hrm.battery_pct is None else f"  ·  battery {sh.hrm.battery_pct}%"))
        else:
            hr = s.heart_rate_bpm if live else None
            hr_sub = "" if hr is None else "via trainer"
        self.hr.set("--" if not hr else f"{hr}", hr_sub)
        if live:
            power = s.power_w if s.power_w is not None else s.cps_power_w
            cad = s.cadence_rpm if s.cadence_rpm is not None else s.crank_cadence_rpm
            self.power.set("--" if power is None else f"{power:.0f}", self._energy_text())
            self.cadence.set("--" if cad is None else f"{cad:.0f}")
            dist = "" if s.distance_m is None else f"distance {s.distance_m / 1000:.2f} km"
            self.speed.set("--" if s.speed_kmh is None else f"{s.speed_kmh:.1f}", dist)
            # same 5-revolution average as the peanut, so the two always agree
            recent = list(sh.revs)[-PEANUT_REVS:]
            if s.balance_left_pct is not None and recent:
                lp = sum(r.left_pct for r in recent) / len(recent)
                lw = None if not power else power * lp / 100
                self.balance.set(lp, lw, None if lw is None else power - lw)
            else:
                self.balance.set(None, None, None)
        else:
            power = cad = None
            for t in (self.power, self.cadence, self.speed):
                t.set("--")
            self.power.set("--", self._energy_text())  # the ride's total stays on show
            self.balance.set(None, None, None)

        if self.workout:
            snap = sh.player.snapshot()
            self.workout.update_view(snap, cad)
            self.overlay.show_message(snap["message"], snap["message_age"])

        if now - self._last_hist >= 0.5:
            self._last_hist = now
            self.history.add(now, power, cad, s.speed_kmh if live else None, hr)

        if sh.rev_count != self._seen_revs:
            self._seen_revs = sh.rev_count
            self.peanut.update_revs(list(sh.revs)[-PEANUT_REVS:])
        elif sh.revs and now - sh.revs[-1].timestamp > STALE_S:
            self.peanut.reset()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.overlay.place()

    def _energy_text(self) -> str:
        """Work done at the pedals this ride, in kJ."""
        if self.shared.stats is None:
            return ""
        kj = self.shared.stats.energy_j / 1000
        return f"{kj:.1f} kJ" if kj < 10 else f"{kj:.0f} kJ"

    def closeEvent(self, ev) -> None:
        self.timer.stop()
        if self.beeper:
            self.beeper.stop()
        self.worker.stop()
        self.worker.join(timeout=5)
        super().closeEvent(ev)


def _apply_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, col in ((QtGui.QPalette.Window, COL_BG), (QtGui.QPalette.Base, COL_PANEL),
                      (QtGui.QPalette.WindowText, COL_TEXT), (QtGui.QPalette.Text, COL_TEXT)):
        pal.setColor(role, QtGui.QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(f"QMainWindow {{ background:{COL_BG}; }}"
                      f"QFrame#tile {{ background:{COL_PANEL}; border:1px solid #21262d; border-radius:10px; }}"
                      f"QLabel {{ background:transparent; border:none; }}")
    pg.setConfigOptions(antialias=True, foreground=COL_TEXT, background=COL_PANEL)


def run(args) -> None:
    shared = Shared()
    try:
        shared.user = tw.load_user(args.user)
        if args.workout:
            workout = tw.load_workout(args.workout)
            ftp = args.ftp or (shared.user.ftp if shared.user else None)
            shared.player = tw.WorkoutPlayer(workout, ftp, autopause=not args.no_autopause)
    except tw.WorkoutError as e:
        raise SystemExit(f"Error: {e}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _apply_theme(app)
    worker = (ReplayWorker if args.replay else TrainerWorker)(shared, args)
    win = Dashboard(shared, worker, beeps=not getattr(args, "no_beeps", False))
    avail = app.primaryScreen().availableGeometry()  # don't open bigger than the screen
    win.resize(min(win.width(), avail.width()), min(win.height(), avail.height()))
    worker.start()
    if args.screenshot:  # render after a while, save a PNG and quit (for testing)
        def snap():
            win.grab().save(args.screenshot)
            win.close()
            app.quit()
        QtCore.QTimer.singleShot(int((args.seconds or 10) * 1000), snap)
    win.show()
    app.exec()
