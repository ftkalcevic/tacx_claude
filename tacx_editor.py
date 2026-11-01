"""
tacx_editor.py - visual editor for structured workout files (see workout_format.md).

Top: the workout drawn as on the dashboard (blocks coloured by power zone at the
user's FTP). Click a block to select its step. Bottom: the step tree, with the
properties of the selected step or repeat block beside it. Every edit is
validated with the same loader the player uses.

    python tacx_neo.py editor workouts\\base_01_easy_spin.workout.json
    python tacx_neo.py editor                       # start a new workout
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from typing import Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

import tacx_workout as tw
from tacx_dashboard import (COL_BG, COL_GRID, COL_NON_ERG, COL_PANEL, COL_RIGHT, COL_TEXT,
                            _apply_theme, zone_colour)

ROLE = QtCore.Qt.UserRole
COL_OK = "#2ec4b6"

TYPES = [  # (key, label, min, max, step, decimals, suffix, default)
    ("ftp_pct", "Power, % of FTP (ERG)", 0, 300, 1, 0, " %", 60),
    ("watts", "Power, watts (ERG)", 0, tw.MAX_ERG_W, 5, 0, " W", 100),
    ("grade", "Slope %", -10, 20, 0.5, 1, " %", 2.0),
    ("resistance", "Resistance %", 0, 100, 1, 0, " %", 20),
    ("free", "Free ride", 0, 0, 1, 0, "", 0),
]
TYPE_INDEX = {t[0]: i for i, t in enumerate(TYPES)}

NEW_WORKOUT = {
    "format": "tacx-workout", "version": 1, "name": "New workout", "author": "",
    "description": "",
    "steps": [
        {"name": "Warm up", "duration": "5:00", "ftp_pct": [40, 60]},
        {"name": "Steady", "duration": "20:00", "ftp_pct": 60, "cadence": [85, 95]},
        {"name": "Cool down", "duration": "5:00", "ftp_pct": [55, 40]},
    ],
}
NEW_STEP = {"name": "Steady", "duration": "5:00", "ftp_pct": 60}
NEW_REPEAT = {"repeat": 3, "name": "Intervals", "steps": [
    {"name": "On", "duration": "1:00", "ftp_pct": 100},
    {"name": "Off", "duration": "1:00", "ftp_pct": 50}]}


def target_key(d: dict) -> Optional[str]:
    return next((k for k in tw.TARGET_KEYS if k in d), None)


def duration_value(text: str):
    """Store plain numbers as seconds and anything with ':' as written."""
    text = text.strip()
    return int(text) if text.isdigit() else text


def expand(items, ancestors=(), groups=()):
    """Yield (source dict, Step or None, ancestor dicts) for every step played, in order.
    Invalid steps yield None, so the chart can still show the rest."""
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if "repeat" in item:
            n = item["repeat"] if isinstance(item["repeat"], int) and item["repeat"] > 0 else 1
            label = item.get("name") or "Repeat"
            for k in range(n):
                yield from expand(item.get("steps"), ancestors + (item,), groups + (f"{label} {k + 1}/{n}",))
        else:
            try:
                step = tw.parse_step(item, "", " · ".join(groups))
            except tw.WorkoutError:
                step = None
            yield item, step, ancestors


# --- Chart --------------------------------------------------------------------
class ProfileChart(pg.PlotWidget):
    """The workout as the dashboard draws it, with the selection highlighted."""

    # index into self.spans of the clicked block (not the dict itself: Qt would pass a copy)
    clicked = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.setBackground(COL_PANEL)
        self.setMouseEnabled(False, False)
        self.setMenuEnabled(False)
        self.hideButtons()
        self.showGrid(x=False, y=True, alpha=0.12)
        self.setLabel("bottom", "minutes")
        self.setLabel("left", "W")
        self.getAxis("left").setWidth(48)
        self._items: list = []
        self.spans: list = []  # (start s, end s, source dict)
        self.ftp_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(COL_GRID, width=1, style=QtCore.Qt.DashLine),
                                        label="FTP", labelOpts={"color": COL_TEXT, "position": 0.02})
        self.addItem(self.ftp_line)
        self.scene().sigMouseClicked.connect(self._on_click)

    def draw(self, doc: dict, ftp: float, selected: Optional[dict]) -> None:
        for it in self._items:
            self.removeItem(it)
        self._items, self.spans = [], []
        t, top = 0.0, ftp
        for src, step, ancestors in expand(doc.get("steps")):
            if step is None:  # invalid step: show a red marker 1 minute wide so it can still be clicked
                step_d, ys, colour = 60.0, [ftp * 0.3] * 2, COL_RIGHT
            else:
                step_d = step.duration
                ys = ([step.watts_at(0, ftp), step.watts_at(step.duration, ftp)] if step.is_erg
                      else [ftp * 0.5] * 2)
                colour = zone_colour(ys[0], ftp) if step.is_erg else COL_NON_ERG
            hit = selected is not None and (src is selected or any(a is selected for a in ancestors))
            fill = QtGui.QColor(colour)
            fill.setAlpha(235 if hit else (150 if selected is not None else 200))
            pen = pg.mkPen("#ffffff", width=2) if hit else pg.mkPen(fill, width=1)
            item = pg.PlotDataItem([t / 60, (t + step_d) / 60], ys, pen=pen, fillLevel=0, brush=fill)
            self.addItem(item)
            self._items.append(item)
            if step is not None:
                for at, _msg in step.texts:  # text cues as small ticks along the bottom
                    tick = pg.ScatterPlotItem([(t + at) / 60], [0], symbol="t1", size=9,
                                              brush="#ffd166", pen=None)
                    self.addItem(tick)
                    self._items.append(tick)
            self.spans.append((t, t + step_d, src))
            top = max(top, *ys)
            t += step_d
        self.ftp_line.setValue(ftp)
        self.setXRange(0, max(t, 60) / 60, padding=0.01)
        self.setYRange(0, top * 1.12, padding=0)

    def _on_click(self, ev) -> None:
        vb = self.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        x = vb.mapSceneToView(ev.scenePos()).x() * 60
        for i, (a, b, _src) in enumerate(self.spans):
            if a <= x < b:
                self.clicked.emit(i)
                return


# --- Property forms -----------------------------------------------------------
class StepForm(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, editor: "Editor"):
        super().__init__()
        self.editor = editor
        self.item: Optional[dict] = None
        self._loading = False
        form = QtWidgets.QFormLayout(self)
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.name = QtWidgets.QLineEdit()
        self.duration = QtWidgets.QLineEdit()
        self.duration.setPlaceholderText("seconds, M:SS or H:MM:SS")
        self.duration_info = QtWidgets.QLabel()
        dur = QtWidgets.QHBoxLayout()
        dur.addWidget(self.duration)
        dur.addWidget(self.duration_info)

        self.kind = QtWidgets.QComboBox()
        for t in TYPES:
            self.kind.addItem(t[1])
        self.ramp = QtWidgets.QCheckBox("Ramp from start to end")
        self.start = QtWidgets.QDoubleSpinBox()
        self.end = QtWidgets.QDoubleSpinBox()
        self.watts_info = QtWidgets.QLabel()
        tgt = QtWidgets.QHBoxLayout()
        tgt.addWidget(self.start)
        self.arrow = QtWidgets.QLabel("→")
        tgt.addWidget(self.arrow)
        tgt.addWidget(self.end)
        tgt.addWidget(self.watts_info, 1)

        self.cad_on = QtWidgets.QCheckBox("Cadence target")
        self.cad_lo = QtWidgets.QSpinBox()
        self.cad_hi = QtWidgets.QSpinBox()
        for s in (self.cad_lo, self.cad_hi):
            s.setRange(20, 200)
            s.setSuffix(" rpm")
        cad = QtWidgets.QHBoxLayout()
        cad.addWidget(self.cad_on)
        cad.addWidget(self.cad_lo)
        cad.addWidget(QtWidgets.QLabel("to"))
        cad.addWidget(self.cad_hi)
        cad.addStretch()

        self.texts = QtWidgets.QTableWidget(0, 2)
        self.texts.setHorizontalHeaderLabels(["At", "Message"])
        self.texts.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.texts.verticalHeader().setVisible(False)
        self.texts.setColumnWidth(0, 70)
        self.texts.setMinimumHeight(110)
        tbtn = QtWidgets.QHBoxLayout()
        self.add_text = QtWidgets.QPushButton("Add message")
        self.del_text = QtWidgets.QPushButton("Remove message")
        tbtn.addWidget(self.add_text)
        tbtn.addWidget(self.del_text)
        tbtn.addStretch()

        form.addRow("Name", self.name)
        form.addRow("Duration", dur)
        form.addRow("Target", self.kind)
        form.addRow("", self.ramp)
        form.addRow("Value", tgt)
        form.addRow("", cad)
        form.addRow("Messages", self.texts)
        form.addRow("", tbtn)

        self.name.textEdited.connect(self._store)
        self.duration.textEdited.connect(self._store)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.ramp.toggled.connect(self._store)
        self.start.valueChanged.connect(self._store)
        self.end.valueChanged.connect(self._store)
        self.cad_on.toggled.connect(self._store)
        self.cad_lo.valueChanged.connect(self._store)
        self.cad_hi.valueChanged.connect(self._store)
        self.texts.cellChanged.connect(self._store)
        self.add_text.clicked.connect(self._add_text)
        self.del_text.clicked.connect(self._del_text)

    def _configure_spins(self, kind: str) -> None:
        _, _, lo, hi, step, dec, suffix, _ = TYPES[TYPE_INDEX[kind]]
        for s in (self.start, self.end):
            s.blockSignals(True)
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(dec)
            s.setSuffix(suffix)
            s.blockSignals(False)
        free = kind == "free"
        erg = kind in ("ftp_pct", "watts")
        self.start.setEnabled(not free)
        self.ramp.setEnabled(erg)
        if not erg:
            self.ramp.setChecked(False)
        self._show_ramp()

    def _show_ramp(self) -> None:
        """The end value only exists for ramps."""
        ramp = self.ramp.isChecked()
        self.end.setVisible(ramp)
        self.arrow.setVisible(ramp)

    def load(self, item: dict) -> None:
        self._loading = True
        self.item = item
        self.name.setText(str(item.get("name", "")))
        d = item.get("duration", "")
        self.duration.setText(str(d))
        kind = target_key(item) or "ftp_pct"
        self.kind.setCurrentIndex(TYPE_INDEX[kind])
        self._configure_spins(kind)
        v = item.get(kind, TYPES[TYPE_INDEX[kind]][7])
        if isinstance(v, list) and len(v) == 2:
            self.ramp.setChecked(True)
            self.start.setValue(float(v[0]))
            self.end.setValue(float(v[1]))
        else:
            self.ramp.setChecked(False)
            val = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
            self.start.setValue(val)
            self.end.setValue(val)
        self._show_ramp()
        cad = item.get("cadence")
        self.cad_on.setChecked(cad is not None)
        lo, hi = (cad if isinstance(cad, list) else [cad - 5, cad + 5]) if cad is not None else [85, 95]
        self.cad_lo.setValue(int(lo))
        self.cad_hi.setValue(int(hi))
        self.cad_lo.setEnabled(cad is not None)
        self.cad_hi.setEnabled(cad is not None)
        self.texts.setRowCount(0)
        for t in item.get("text", []):
            r = self.texts.rowCount()
            self.texts.insertRow(r)
            self.texts.setItem(r, 0, QtWidgets.QTableWidgetItem(str(t.get("at", ""))))
            self.texts.setItem(r, 1, QtWidgets.QTableWidgetItem(str(t.get("msg", ""))))
        self._loading = False
        self._update_info()

    def _kind_changed(self) -> None:
        if self._loading or self.item is None:
            return
        old = target_key(self.item)
        new = TYPES[self.kind.currentIndex()][0]
        ftp = self.editor.ftp
        val = TYPES[TYPE_INDEX[new]][7]
        cur = self.item.get(old)
        if old == "ftp_pct" and new == "watts" and cur is not None:  # keep the same watts
            val = [round(x * ftp / 100 / 5) * 5 for x in cur] if isinstance(cur, list) else round(cur * ftp / 100 / 5) * 5
        elif old == "watts" and new == "ftp_pct" and cur is not None:
            val = [round(x * 100 / ftp) for x in cur] if isinstance(cur, list) else round(cur * 100 / ftp)
        for k in tw.TARGET_KEYS:
            self.item.pop(k, None)
        self.item[new] = True if new == "free" else val
        self.load(self.item)
        self.changed.emit()

    def _store(self) -> None:
        if self._loading or self.item is None:
            return
        it = self.item
        name = self.name.text().strip()
        if name:
            it["name"] = name
        else:
            it.pop("name", None)
        it["duration"] = duration_value(self.duration.text())
        kind = TYPES[self.kind.currentIndex()][0]
        self._show_ramp()
        if kind != "free":
            num = lambda x: int(x) if float(x).is_integer() else round(x, 1)
            it[kind] = ([num(self.start.value()), num(self.end.value())] if self.ramp.isChecked()
                        else num(self.start.value()))
        self.cad_lo.setEnabled(self.cad_on.isChecked())
        self.cad_hi.setEnabled(self.cad_on.isChecked())
        if self.cad_on.isChecked():
            it["cadence"] = [self.cad_lo.value(), self.cad_hi.value()]
        else:
            it.pop("cadence", None)
        texts = []
        for r in range(self.texts.rowCount()):
            at = self.texts.item(r, 0)
            msg = self.texts.item(r, 1)
            texts.append({"at": duration_value(at.text() if at else "0"), "msg": msg.text() if msg else ""})
        if texts:
            it["text"] = texts
        else:
            it.pop("text", None)
        self._update_info()
        self.changed.emit()

    def _update_info(self) -> None:
        try:
            d = tw.parse_duration(self.duration.text() and duration_value(self.duration.text()), "")
            self.duration_info.setText(f"= {tw.fmt_time(d)}")
            self.duration_info.setStyleSheet(f"color:{COL_TEXT};")
        except tw.WorkoutError:
            self.duration_info.setText("not a valid duration")
            self.duration_info.setStyleSheet(f"color:{COL_RIGHT};")
        kind = TYPES[self.kind.currentIndex()][0]
        ftp = self.editor.ftp
        if kind == "ftp_pct":
            a, b = self.start.value() * ftp / 100, self.end.value() * ftp / 100
            self.watts_info.setText(f"= {a:.0f} W" + (f" → {b:.0f} W" if self.ramp.isChecked() else "")
                                    + f"  at FTP {ftp:.0f} W")
        elif kind == "watts":
            self.watts_info.setText(f"= {100 * self.start.value() / ftp:.0f}% of FTP {ftp:.0f} W")
        else:
            self.watts_info.setText("")

    def _add_text(self) -> None:
        if self.item is None:
            return
        self.item.setdefault("text", []).append({"at": "0:00", "msg": "Message"})
        self.load(self.item)
        self.changed.emit()

    def _del_text(self) -> None:
        r = self.texts.currentRow()
        if self.item is None or r < 0 or "text" not in self.item:
            return
        del self.item["text"][r]
        if not self.item["text"]:
            del self.item["text"]
        self.load(self.item)
        self.changed.emit()


class RepeatForm(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.item: Optional[dict] = None
        self._loading = False
        form = QtWidgets.QFormLayout(self)
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        self.name = QtWidgets.QLineEdit()
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 100)
        self.count.setSuffix(" times")
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        form.addRow("Block name", self.name)
        form.addRow("Repeat", self.count)
        form.addRow("", self.info)
        self.name.textEdited.connect(self._store)
        self.count.valueChanged.connect(self._store)

    def load(self, item: dict) -> None:
        self._loading = True
        self.item = item
        self.name.setText(str(item.get("name", "")))
        self.count.setValue(item.get("repeat", 1) if isinstance(item.get("repeat"), int) else 1)
        self._loading = False
        self._update_info()

    def _store(self) -> None:
        if self._loading or self.item is None:
            return
        name = self.name.text().strip()
        if name:
            self.item["name"] = name
        else:
            self.item.pop("name", None)
        self.item["repeat"] = self.count.value()
        self._update_info()
        self.changed.emit()

    def _update_info(self) -> None:
        once = sum(s.duration for _, s, _ in expand(self.item.get("steps")) if s) / max(1, self.item["repeat"])
        self.info.setText(f"{len(self.item.get('steps', []))} steps per repeat, "
                          f"{tw.fmt_time(once)} each, {tw.fmt_time(once * self.item['repeat'])} in total.\n"
                          "Select a step inside the block to edit it.")


# --- Main window --------------------------------------------------------------
class Editor(QtWidgets.QMainWindow):
    def __init__(self, path: Optional[str], ftp: float, ftp_note: str):
        super().__init__()
        self.ftp = ftp
        self.ftp_note = ftp_note
        self.path: Optional[str] = None
        self.doc: dict = {}
        self.dirty = False
        self.selected: Optional[dict] = None
        self.resize(1300, 900)

        self.chart = ProfileChart()
        self.chart.clicked.connect(lambda i: self.select(self.chart.spans[i][2]))
        self.header = QtWidgets.QLabel()
        self.header.setStyleSheet(f"color:{COL_TEXT}; font-size:12pt; letter-spacing:1px;")
        top = QtWidgets.QFrame()
        top.setObjectName("tile")
        tl = QtWidgets.QVBoxLayout(top)
        tl.addWidget(self.header)
        tl.addWidget(self.chart, 1)

        # workout details + step tree
        self.wname = QtWidgets.QLineEdit()
        self.wauthor = QtWidgets.QLineEdit()
        self.wdesc = QtWidgets.QPlainTextEdit()
        self.wdesc.setFixedHeight(54)
        meta = QtWidgets.QFormLayout()
        meta.addRow("Workout", self.wname)
        meta.addRow("Author", self.wauthor)
        meta.addRow("Description", self.wdesc)
        self.wname.textEdited.connect(self._store_meta)
        self.wauthor.textEdited.connect(self._store_meta)
        self.wdesc.textChanged.connect(self._store_meta)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Step", "Duration", "Target"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 80)
        self.tree.itemSelectionChanged.connect(self._tree_selected)
        left = QtWidgets.QFrame()
        left.setObjectName("tile")
        ll = QtWidgets.QVBoxLayout(left)
        ll.addLayout(meta)
        ll.addWidget(self.tree, 1)

        self.step_form = StepForm(self)
        self.repeat_form = RepeatForm()
        self.step_form.changed.connect(self._edited)
        self.repeat_form.changed.connect(self._edited)
        empty = QtWidgets.QLabel("Select a step in the tree or click one on the chart.")
        empty.setAlignment(QtCore.Qt.AlignCenter)
        self.forms = QtWidgets.QStackedWidget()
        for w in (empty, self.step_form, self.repeat_form):
            self.forms.addWidget(w)
        self.form_title = QtWidgets.QLabel("PROPERTIES")
        self.form_title.setStyleSheet(f"color:{COL_TEXT}; font-size:11pt; letter-spacing:2px;")
        right = QtWidgets.QFrame()
        right.setObjectName("tile")
        rl = QtWidgets.QVBoxLayout(right)
        rl.addWidget(self.form_title)
        rl.addWidget(self.forms, 1)

        bottom = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        bottom.addWidget(left)
        bottom.addWidget(right)
        bottom.setSizes([520, 780])
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([330, 570])
        central = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(central)
        cl.setContentsMargins(10, 6, 10, 6)
        cl.addWidget(split)
        self.setCentralWidget(central)

        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        self._make_toolbar()
        if path:
            self.open_path(path)
        else:
            self.new()

    # ----- toolbar -----
    def _make_toolbar(self) -> None:
        tb = self.addToolBar("Edit")
        tb.setMovable(False)
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)

        def act(text, slot, shortcut=None, tip=""):
            a = QtGui.QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QtGui.QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        act("New", self.new, "Ctrl+N")
        act("Open…", self.open_dialog, "Ctrl+O")
        act("Save", self.save, "Ctrl+S")
        act("Save as…", self.save_as, "Ctrl+Shift+S")
        tb.addSeparator()
        act("+ Step", lambda: self.insert(copy.deepcopy(NEW_STEP)), "Ctrl+T", "Add a step after the selection")
        act("+ Repeat block", lambda: self.insert(copy.deepcopy(NEW_REPEAT)), "Ctrl+R",
            "Add a repeat block after the selection")
        act("Duplicate", self.duplicate, "Ctrl+D")
        act("Delete", self.delete)
        # Del deletes a step only from the tree, so typing in a field never removes steps
        del_key = QtGui.QShortcut(QtGui.QKeySequence.Delete, self.tree)
        del_key.setContext(QtCore.Qt.WidgetShortcut)
        del_key.activated.connect(self.delete)
        act("▲ Up", lambda: self.move(-1), "Ctrl+Up")
        act("▼ Down", lambda: self.move(1), "Ctrl+Down")
        tb.addSeparator()
        act("Ride it ▶", self.ride, None, "Save, then open this workout in the dashboard")

    # ----- file handling -----
    def new(self) -> None:
        if not self._confirm_discard():
            return
        self._set_doc(copy.deepcopy(NEW_WORKOUT), None)

    def open_dialog(self) -> None:
        if not self._confirm_discard():
            return
        start = os.path.dirname(self.path) if self.path else os.path.join(os.path.dirname(__file__), "workouts")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open workout", start,
                                                        "Workouts (*.workout.json *.json *.zwo)")
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        try:
            if path.lower().endswith(".zwo"):
                doc, keep = tw._zwo_items(path), None  # imported: saving asks for a new .json name
            else:
                with open(path, encoding="utf-8") as f:
                    doc, keep = json.load(f), path
        except (OSError, ValueError, tw.WorkoutError) as e:
            QtWidgets.QMessageBox.critical(self, "Open workout", f"Couldn't open {path}:\n{e}")
            return
        if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
            QtWidgets.QMessageBox.critical(self, "Open workout", f"{path} isn't a workout file.")
            return
        self._set_doc(doc, keep)

    def save(self) -> bool:
        if not self.path:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        start = self.path or os.path.join(os.path.dirname(__file__), "workouts",
                                          _slug(self.doc.get("name", "workout")) + ".workout.json")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save workout", start, "Workouts (*.workout.json)")
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".workout.json"
        return self._write(path)

    def _write(self, path: str) -> bool:
        err = self._validate()
        if err and QtWidgets.QMessageBox.question(
                self, "Save workout", f"The workout has an error:\n{err}\n\nSave anyway?") \
                != QtWidgets.QMessageBox.Yes:
            return False
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tw.with_schema(self.doc, tw.WORKOUT_SCHEMA, path), f, indent=2, ensure_ascii=False)
            f.write("\n")
        self.path = path
        self.dirty = False
        self._refresh()
        return True

    def ride(self) -> None:
        if (self.dirty or not self.path) and not self.save():
            return
        if self._validate():
            QtWidgets.QMessageBox.warning(self, "Ride", "Fix the workout's errors first.")
            return
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tacx_neo.py")
        subprocess.Popen([sys.executable, script, "dashboard", "--workout", self.path])

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Unsaved changes", "Save changes to this workout first?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            return self.save()
        return r == QtWidgets.QMessageBox.Discard

    def closeEvent(self, ev) -> None:
        if self._confirm_discard():
            ev.accept()
        else:
            ev.ignore()

    def _set_doc(self, doc: dict, path: Optional[str]) -> None:
        self.doc, self.path, self.dirty = doc, path, False
        for w, v in ((self.wname, doc.get("name", "")), (self.wauthor, doc.get("author", ""))):
            w.blockSignals(True)
            w.setText(str(v))
            w.blockSignals(False)
        self.wdesc.blockSignals(True)
        self.wdesc.setPlainText(str(doc.get("description", "")))
        self.wdesc.blockSignals(False)
        self.selected = None
        self._rebuild_tree()
        self.forms.setCurrentIndex(0)
        self._refresh()

    # ----- structure -----
    def _owner(self, target: dict, items=None) -> Optional[list]:
        """The list that directly contains `target`."""
        items = self.doc["steps"] if items is None else items
        for it in items:
            if it is target:
                return items
            if isinstance(it, dict) and isinstance(it.get("steps"), list):
                found = self._owner(target, it["steps"])
                if found is not None:
                    return found
        return None

    def insert(self, new: dict) -> None:
        owner = self._owner(self.selected) if self.selected is not None else None
        if owner is None:
            self.doc["steps"].append(new)
        else:
            owner.insert(owner.index(self.selected) + 1, new)
        self._structure_changed(new)

    def duplicate(self) -> None:
        if self.selected is not None:
            self.insert(copy.deepcopy(self.selected))

    def delete(self) -> None:
        if self.selected is None:
            return
        target = self.selected
        owner = self._owner(target)
        if owner is None:
            return
        i = owner.index(target)
        owner.remove(target)
        # a repeat block left empty goes too
        while not owner and owner is not self.doc["steps"]:
            block = next(b for b in self._all_blocks() if b.get("steps") is owner)
            parent = self._owner(block)
            parent.remove(block)
            owner = parent
        nxt = owner[min(i, len(owner) - 1)] if owner else None
        self._structure_changed(nxt)

    def move(self, delta: int) -> None:
        if self.selected is None:
            return
        owner = self._owner(self.selected)
        i = owner.index(self.selected)
        j = i + delta
        if 0 <= j < len(owner):
            owner[i], owner[j] = owner[j], owner[i]
            self._structure_changed(self.selected)

    def _all_blocks(self, items=None):
        for it in self.doc["steps"] if items is None else items:
            if isinstance(it, dict) and "repeat" in it:
                yield it
                yield from self._all_blocks(it.get("steps", []))

    def _structure_changed(self, select: Optional[dict]) -> None:
        self.dirty = True
        self._rebuild_tree()
        self.select(select)

    # ----- tree -----
    def _obj(self, node: QtWidgets.QTreeWidgetItem) -> dict:
        return self._objs[node.data(0, ROLE)]

    def _rebuild_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        self._objs: list = []

        def add(parent, items):
            for it in items:
                node = QtWidgets.QTreeWidgetItem(parent)
                node.setData(0, ROLE, len(self._objs))  # index, not the dict: Qt would store a copy
                self._objs.append(it)
                if "repeat" in it:
                    add(node, it.get("steps", []))
                    node.setExpanded(True)
        add(self.tree.invisibleRootItem(), self.doc.get("steps", []))
        self.tree.blockSignals(False)
        self._update_tree_text()

    def _update_tree_text(self) -> None:
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            node = it.value()
            d = self._obj(node)
            if "repeat" in d:
                once = sum(s.duration for _, s, _ in expand(d.get("steps")) if s)
                node.setText(0, f"⟳ {d.get('name', 'Repeat')}  × {d.get('repeat')}")
                node.setText(1, tw.fmt_time(once))
                node.setText(2, f"{len(d.get('steps', []))} steps per repeat")
                node.setForeground(0, QtGui.QBrush(QtGui.QColor(COL_TEXT)))
            else:
                try:
                    s = tw.parse_step(d, "", "")
                    node.setText(0, s.name)
                    node.setText(1, tw.fmt_time(s.duration))
                    node.setText(2, s.describe(self.ftp))
                    c = zone_colour(s.watts_at(0, self.ftp), self.ftp) if s.is_erg else COL_NON_ERG
                    node.setForeground(2, QtGui.QBrush(QtGui.QColor(c)))
                    node.setForeground(0, QtGui.QBrush(QtGui.QColor(COL_TEXT)))
                    node.setToolTip(0, "")
                except tw.WorkoutError as e:
                    node.setText(0, f"⚠ {d.get('name', 'Step')}")
                    node.setText(1, str(d.get("duration", "?")))
                    node.setText(2, "error")
                    node.setForeground(0, QtGui.QBrush(QtGui.QColor(COL_RIGHT)))
                    node.setToolTip(0, str(e).lstrip(": "))
            it += 1

    def _node_for(self, target: dict) -> Optional[QtWidgets.QTreeWidgetItem]:
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            if self._obj(it.value()) is target:
                return it.value()
            it += 1
        return None

    def select(self, target: Optional[dict]) -> None:
        node = self._node_for(target) if target is not None else None
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        if node:
            node.setSelected(True)
            self.tree.scrollToItem(node)
        self.tree.blockSignals(False)
        self._show(target if node else None)

    def _tree_selected(self) -> None:
        nodes = self.tree.selectedItems()
        self._show(self._obj(nodes[0]) if nodes else None)

    def _show(self, target: Optional[dict]) -> None:
        self.selected = target
        if target is None:
            self.forms.setCurrentIndex(0)
            self.form_title.setText("PROPERTIES")
        elif "repeat" in target:
            self.repeat_form.load(target)
            self.forms.setCurrentIndex(2)
            self.form_title.setText("REPEAT BLOCK")
        else:
            self.step_form.load(target)
            self.forms.setCurrentIndex(1)
            self.form_title.setText("STEP")
        self._refresh()

    # ----- edits -----
    def _store_meta(self) -> None:
        self.doc["name"] = self.wname.text()
        for key, val in (("author", self.wauthor.text()), ("description", self.wdesc.toPlainText())):
            if val.strip():
                self.doc[key] = val
            else:
                self.doc.pop(key, None)
        self._edited()

    def _edited(self) -> None:
        self.dirty = True
        self._update_tree_text()
        self._refresh()

    def _validate(self) -> Optional[str]:
        try:
            w = tw.parse_workout(self.doc, self.path or "")
            tw.WorkoutPlayer(w, self.ftp)
            return None
        except tw.WorkoutError as e:
            return str(e)

    def _refresh(self) -> None:
        self.chart.draw(self.doc, self.ftp, self.selected)
        total = sum(s.duration for _, s, _ in expand(self.doc.get("steps")) if s)
        count = sum(1 for _ in expand(self.doc.get("steps")))
        name = self.doc.get("name") or "Untitled"
        self.header.setText(f"{name.upper()}  ·  {tw.fmt_time(total)}  ·  {count} steps  ·  {self.ftp_note}")
        err = self._validate()
        if err:
            self.status.setText(f"⚠  {err}")
            self.status.setStyleSheet(f"color:{COL_RIGHT};")
        else:
            self.status.setText("✓  Valid workout")
            self.status.setStyleSheet(f"color:{COL_OK};")
        fname = os.path.basename(self.path) if self.path else "unsaved"
        self.setWindowTitle(f"{'• ' if self.dirty else ''}{fname} - Tacx Workout Editor")


def _slug(name: str) -> str:
    s = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
    return "_".join(p for p in s.split("_") if p) or "workout"


def apply_editor_theme(app: QtWidgets.QApplication) -> None:
    """The dashboard theme plus dark styling for the editor's form controls."""
    _apply_theme(app)
    app.setStyleSheet(app.styleSheet() +
                      f"QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTableWidget, QTreeWidget"
                      f" {{ background:{COL_BG}; color:{COL_TEXT}; border:1px solid #30363d; border-radius:4px;"
                      f" padding:3px; }}"
                      f"QTreeWidget::item:selected, QTableWidget::item:selected {{ background:#1f6feb; }}"
                      f"QHeaderView::section {{ background:{COL_PANEL}; color:{COL_TEXT}; border:none; padding:4px; }}"
                      f"QTableCornerButton::section {{ background:{COL_PANEL}; border:none; }}"
                      f"QToolBar {{ background:{COL_PANEL}; border:none; spacing:4px; padding:4px; }}"
                      f"QToolButton {{ color:{COL_TEXT}; padding:5px 10px; border-radius:4px; }}"
                      f"QToolButton:hover {{ background:#30363d; }}"
                      f"QPushButton {{ color:{COL_TEXT}; background:#21262d; border:1px solid #30363d;"
                      f" border-radius:4px; padding:4px 10px; }}"
                      f"QPushButton:hover {{ background:#30363d; }}"
                      f"QCheckBox, QLabel {{ color:{COL_TEXT}; }}"
                      f"QComboBox QAbstractItemView {{ background:{COL_BG}; color:{COL_TEXT};"
                      f" selection-background-color:#1f6feb; }}")


def run(args) -> None:
    try:
        user = tw.load_user(getattr(args, "user", None))
    except tw.WorkoutError as e:
        raise SystemExit(f"Error: {e}")
    if getattr(args, "ftp", None):
        ftp, note = args.ftp, f"FTP {args.ftp:.0f} W (--ftp)"
    elif user:
        ftp, note = user.ftp, f"FTP {user.ftp:.0f} W ({user.display_name})"
    else:
        ftp, note = 200.0, "FTP 200 W (no user.json - example only)"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_editor_theme(app)
    win = Editor(getattr(args, "file", None), ftp, note)
    avail = app.primaryScreen().availableGeometry()
    win.resize(min(win.width(), avail.width()), min(win.height(), avail.height()))
    if getattr(args, "screenshot", None):  # testing: save a PNG after a moment and quit
        def snap():
            win.grab().save(args.screenshot)
            win.dirty = False
            app.quit()
        QtCore.QTimer.singleShot(1500, snap)
    win.show()
    app.exec()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Tacx workout editor")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--user")
    ap.add_argument("--ftp", type=float)
    run(ap.parse_args())
