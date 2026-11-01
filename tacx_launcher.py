"""
tacx_launcher.py - pick a rider and a workout, then ride or edit it.

Lists riders (users\\*.json, plus user.json) and workouts (workouts\\*.workout.json
and *.zwo), each with a picture of its profile. Thumbnails are cached in
.cache\\thumbnails, named by a hash of the workout file's contents and the
rider's FTP, so a picture is only redrawn when the workout changes. The last
rider and workout are remembered in .cache\\launcher.json.

    python tacx_neo.py launcher
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Optional

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import tacx_workout as tw
from tacx_dashboard import COL_BG, COL_CADENCE, COL_NON_ERG, COL_RIGHT, COL_TEXT, zone_colour
from tacx_editor import _slug, apply_editor_theme

HERE = os.path.dirname(os.path.abspath(__file__))
WORKOUT_DIR = os.path.join(HERE, "workouts")
USER_DIR = os.path.join(HERE, "users")
CACHE_DIR = os.path.join(HERE, ".cache")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbnails")
STATE_FILE = os.path.join(CACHE_DIR, "launcher.json")
THUMB_SIZE = (340, 92)
RENDER_VERSION = 1      # bump when the drawing changes, so cached images are redrawn
NON_ERG_PCT = 55        # assumed effort of slope / resistance / free-ride steps for the load estimate
ROLE_PATH = QtCore.Qt.UserRole


# --- Thumbnails ---------------------------------------------------------------
def render_thumbnail(workout: tw.Workout, ftp: float) -> QtGui.QImage:
    """The workout profile, drawn like the dashboard chart, as an image."""
    w, h = THUMB_SIZE
    img = QtGui.QImage(w, h, QtGui.QImage.Format_ARGB32_Premultiplied)
    img.fill(QtGui.QColor(COL_BG))
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    total = workout.total_s or 1
    tops = [ftp * 1.15]
    for s in workout.steps:
        if s.is_erg:
            tops += [s.watts_at(0, ftp), s.watts_at(s.duration, ftp)]
    top = max(tops) * 1.05
    y = lambda watts: h - 2 - (h - 6) * watts / top
    t = 0.0
    for s in workout.steps:
        x0, x1 = (w - 1) * t / total, (w - 1) * (t + s.duration) / total
        if s.is_erg:
            a, b = s.watts_at(0, ftp), s.watts_at(s.duration, ftp)
            colour = zone_colour(a, ftp)
        else:
            a = b = ftp * 0.5
            colour = COL_NON_ERG
        c = QtGui.QColor(colour)
        c.setAlpha(225)
        poly = QtGui.QPolygonF([QtCore.QPointF(x0, h), QtCore.QPointF(x0, y(a)),
                                QtCore.QPointF(x1, y(b)), QtCore.QPointF(x1, h)])
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawPolygon(poly)
        t += s.duration
    pen = QtGui.QPen(QtGui.QColor(COL_TEXT), 1, QtCore.Qt.DashLine)
    pen.setColor(QtGui.QColor(201, 209, 217, 110))
    p.setPen(pen)
    p.drawLine(QtCore.QPointF(0, y(ftp)), QtCore.QPointF(w, y(ftp)))
    p.end()
    return img


class ThumbnailCache:
    """PNG files named by a hash of (workout file bytes, FTP, renderer version)."""

    def __init__(self, folder: str = THUMB_DIR):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.rendered = 0  # how many were drawn (rather than loaded) since startup

    def key(self, path: str, ftp: float) -> str:
        with open(path, "rb") as f:
            data = f.read()
        return hashlib.sha1(data + f"|{ftp:.1f}|{RENDER_VERSION}".encode()).hexdigest()

    def get(self, path: str, workout: tw.Workout, ftp: float) -> QtGui.QPixmap:
        png = os.path.join(self.folder, self.key(path, ftp) + ".png")
        if os.path.isfile(png):
            os.utime(png)  # mark as recently used, for prune()
            pm = QtGui.QPixmap(png)
            if not pm.isNull():
                return pm
        img = render_thumbnail(workout, ftp)
        img.save(png)
        self.rendered += 1
        return QtGui.QPixmap.fromImage(img)

    def prune(self, max_age_days: int = 60) -> None:
        """Remove pictures of old versions of workouts that haven't been shown for a while."""
        limit = time.time() - max_age_days * 86400
        for f in glob.glob(os.path.join(self.folder, "*.png")):
            if os.path.getmtime(f) < limit:
                try:
                    os.remove(f)
                except OSError:
                    pass


def workout_load(workout: tw.Workout, ftp: float) -> tuple:
    """(intensity factor, estimated TSS), from a 1-second power trace of the targets."""
    secs = []
    for s in workout.steps:
        n = max(1, int(round(s.duration)))
        if s.is_erg:
            secs.append(np.array([s.watts_at(i, ftp) for i in range(n)], dtype=float))
        else:
            secs.append(np.full(n, ftp * NON_ERG_PCT / 100))
    p = np.concatenate(secs)
    if len(p) >= 30:  # normalised power: 4th-power mean of the 30 s rolling average
        p = np.convolve(p, np.ones(30) / 30, mode="valid")
    np_w = float(np.mean(p ** 4) ** 0.25)
    intensity = np_w / ftp
    tss = workout.total_s / 3600 * intensity ** 2 * 100
    return intensity, tss


# --- State --------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def user_files() -> list:
    files = sorted(glob.glob(os.path.join(USER_DIR, "*.json")))
    root = os.path.join(HERE, "user.json")
    return ([root] if os.path.isfile(root) else []) + files


def workout_files() -> list:
    files = glob.glob(os.path.join(WORKOUT_DIR, "*.workout.json")) + glob.glob(os.path.join(WORKOUT_DIR, "*.zwo"))
    return sorted(files, key=lambda f: os.path.basename(f).lower())


# --- Rider dialog -------------------------------------------------------------
class RiderDialog(QtWidgets.QDialog):
    """Create or edit a user file."""

    def __init__(self, parent, path: Optional[str] = None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("Edit rider" if path else "New rider")
        self.setMinimumWidth(420)
        d = {}
        if path:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        form = QtWidgets.QFormLayout(self)

        def spin(lo, hi, val, suffix, decimals=0, unset=False):
            s = QtWidgets.QDoubleSpinBox() if decimals else QtWidgets.QSpinBox()
            if decimals:
                s.setDecimals(decimals)
            s.setRange(0 if unset else lo, hi)
            if unset:
                s.setSpecialValueText("not set")
            s.setSuffix(suffix)
            s.setValue(val if val is not None else (0 if unset else lo))
            return s

        self.name = QtWidgets.QLineEdit(d.get("name", ""))
        self.nickname = QtWidgets.QLineEdit(d.get("nickname", ""))
        self.dob = QtWidgets.QLineEdit(d.get("dob", ""))
        self.dob.setPlaceholderText("YYYY-MM-DD (optional)")
        self.sex = QtWidgets.QComboBox()
        self.sex.addItems(["not set", "male", "female"])
        self.sex.setCurrentText(d.get("sex", "not set"))
        self.weight = spin(30, 200, d.get("weight_kg", 75.0), " kg", decimals=1)
        self.height_cm = spin(100, 230, d.get("height_cm"), " cm", unset=True)
        self.ftp = spin(50, 600, d.get("ftp", 150), " W")
        self.ftp_tested = QtWidgets.QLineEdit(d.get("ftp_tested", ""))
        self.ftp_tested.setPlaceholderText("YYYY-MM-DD (optional)")
        self.max_hr = spin(120, 230, d.get("max_hr"), " bpm", unset=True)
        self.resting_hr = spin(30, 100, d.get("resting_hr"), " bpm", unset=True)
        self.bike = spin(3, 40, d.get("bike_weight_kg", 8.0), " kg", decimals=1)
        for label, wdg in (("Name *", self.name), ("Nickname", self.nickname), ("Date of birth", self.dob),
                           ("Sex", self.sex), ("Weight *", self.weight), ("Height", self.height_cm),
                           ("FTP *", self.ftp), ("FTP tested", self.ftp_tested), ("Max HR", self.max_hr),
                           ("Resting HR", self.resting_hr), ("Bike weight", self.bike)):
            form.addRow(label, wdg)
        self.error = QtWidgets.QLabel()
        self.error.setStyleSheet(f"color:{COL_RIGHT};")
        self.error.setWordWrap(True)
        form.addRow(self.error)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _doc(self) -> dict:
        d = {"format": "tacx-user", "version": 1, "name": self.name.text().strip()}
        if self.nickname.text().strip():
            d["nickname"] = self.nickname.text().strip()
        if self.dob.text().strip():
            d["dob"] = self.dob.text().strip()
        if self.sex.currentText() != "not set":
            d["sex"] = self.sex.currentText()
        d["weight_kg"] = round(self.weight.value(), 1)
        if self.height_cm.value():
            d["height_cm"] = self.height_cm.value()
        d["ftp"] = self.ftp.value()
        if self.ftp_tested.text().strip():
            d["ftp_tested"] = self.ftp_tested.text().strip()
        if self.max_hr.value():
            d["max_hr"] = self.max_hr.value()
        if self.resting_hr.value():
            d["resting_hr"] = self.resting_hr.value()
        d["bike_weight_kg"] = round(self.bike.value(), 1)
        return d

    def _save(self) -> None:
        d = self._doc()
        try:
            tw.parse_user(d, self.path or "rider")
        except tw.WorkoutError as e:
            self.error.setText(str(e).split(": ", 1)[-1])
            return
        if not self.path:
            os.makedirs(USER_DIR, exist_ok=True)
            base = _slug(d.get("nickname") or d["name"])
            self.path = os.path.join(USER_DIR, base + ".json")
            n = 2
            while os.path.exists(self.path):
                self.path = os.path.join(USER_DIR, f"{base}_{n}.json")
                n += 1
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(tw.with_schema(d, tw.USER_SCHEMA, self.path), f, indent=2, ensure_ascii=False)
            f.write("\n")
        self.accept()


# --- Workout row --------------------------------------------------------------
class WorkoutRow(QtWidgets.QWidget):
    ride = QtCore.Signal(str)
    edit = QtCore.Signal(str)

    def __init__(self, path: str, pixmap: Optional[QtGui.QPixmap], title: str, meta: str,
                 description: str, error: Optional[str] = None):
        super().__init__()
        self.path = path
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        thumb = QtWidgets.QLabel()
        thumb.setFixedSize(*THUMB_SIZE)
        if pixmap is not None:
            thumb.setPixmap(pixmap)
        else:
            thumb.setStyleSheet(f"background:{COL_BG}; border:1px dashed {COL_RIGHT};")
            thumb.setAlignment(QtCore.Qt.AlignCenter)
            thumb.setText("can't load")
        lay.addWidget(thumb)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        t = QtWidgets.QLabel(title)
        t.setStyleSheet(f"color:{COL_TEXT}; font-size:13pt; font-weight:600;")
        m = QtWidgets.QLabel(meta)
        m.setStyleSheet(f"color:{COL_CADENCE if not error else COL_RIGHT}; font-size:10pt;")
        m.setWordWrap(True)
        dsc = QtWidgets.QLabel(description)
        dsc.setStyleSheet("color:#8b949e; font-size:10pt;")
        dsc.setWordWrap(True)
        text.addWidget(t)
        text.addWidget(m)
        text.addWidget(dsc, 1)
        lay.addLayout(text, 1)
        btns = QtWidgets.QVBoxLayout()
        self.ride_btn = QtWidgets.QPushButton("Ride ▶")
        self.ride_btn.setObjectName("ride")
        self.ride_btn.setEnabled(error is None)
        edit_btn = QtWidgets.QPushButton("Edit")
        self.ride_btn.clicked.connect(lambda: self.ride.emit(self.path))
        edit_btn.clicked.connect(lambda: self.edit.emit(self.path))
        btns.addWidget(self.ride_btn)
        btns.addWidget(edit_btn)
        btns.addStretch()
        lay.addLayout(btns)


# --- Main window --------------------------------------------------------------
class Launcher(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tacx NEO")
        self.resize(1180, 820)
        self.cache = ThumbnailCache()
        self.state = load_state()
        self.users: dict = {}  # path -> UserProfile or error string

        # riders
        self.user_list = QtWidgets.QListWidget()
        self.user_list.currentRowChanged.connect(self._user_changed)
        self.user_list.itemDoubleClicked.connect(lambda _: self.edit_user())
        new_u = QtWidgets.QPushButton("New rider")
        edit_u = QtWidgets.QPushButton("Edit rider")
        new_u.clicked.connect(self.new_user)
        edit_u.clicked.connect(self.edit_user)
        ub = QtWidgets.QHBoxLayout()
        ub.addWidget(new_u)
        ub.addWidget(edit_u)
        self.hrm = QtWidgets.QCheckBox("Heart rate strap")
        self.hrm.setChecked(self.state.get("hrm", False))
        self.autopause = QtWidgets.QCheckBox("Pause when I stop pedalling")
        self.autopause.setChecked(self.state.get("autopause", True))
        self.beeps = QtWidgets.QCheckBox("Countdown beeps")
        self.beeps.setChecked(self.state.get("beeps", True))
        self.beeps.toggled.connect(self._save_state)
        self.hrm.toggled.connect(self._save_state)
        self.autopause.toggled.connect(self._save_state)
        free = QtWidgets.QPushButton("Free ride ▶")
        free.setObjectName("ride")
        free.clicked.connect(self.free_ride)
        new_w = QtWidgets.QPushButton("New workout")
        new_w.clicked.connect(lambda: self.edit_workout(None))

        left = QtWidgets.QFrame()
        left.setObjectName("tile")
        left.setFixedWidth(300)
        ll = QtWidgets.QVBoxLayout(left)
        ll.addWidget(self._caption("Rider"))
        ll.addWidget(self.user_list, 1)
        ll.addLayout(ub)
        ll.addSpacing(10)
        ll.addWidget(self._caption("Options"))
        ll.addWidget(self.hrm)
        ll.addWidget(self.autopause)
        ll.addWidget(self.beeps)
        ll.addSpacing(10)
        ll.addWidget(free)
        ll.addWidget(new_w)

        # workouts
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter workouts…")
        self.filter.textChanged.connect(self._apply_filter)
        self.list = QtWidgets.QListWidget()
        self.list.setSpacing(3)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.currentRowChanged.connect(lambda _: self._save_state())
        self.list.itemDoubleClicked.connect(lambda it: self.ride(it.data(ROLE_PATH)))
        right = QtWidgets.QFrame()
        right.setObjectName("tile")
        rl = QtWidgets.QVBoxLayout(right)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self._caption("Workouts"))
        head.addStretch()
        head.addWidget(self.filter)
        rl.addLayout(head)
        rl.addWidget(self.list, 1)

        central = QtWidgets.QWidget()
        cl = QtWidgets.QHBoxLayout(central)
        cl.setContentsMargins(12, 12, 12, 8)
        cl.addWidget(left)
        cl.addWidget(right, 1)
        self.setCentralWidget(central)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)

        # pick up new and edited files (e.g. saved from the editor) automatically
        self.watcher = QtCore.QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _: self._debounce.start())
        self.watcher.fileChanged.connect(lambda _: self._debounce.start())
        self._debounce = QtCore.QTimer(self, singleShot=True, interval=400)
        self._debounce.timeout.connect(self.refresh)

        self._loading = False
        self.refresh_users(select=self.state.get("user"))
        self.cache.prune()

    @staticmethod
    def _caption(text: str) -> QtWidgets.QLabel:
        l = QtWidgets.QLabel(text.upper())
        l.setStyleSheet(f"color:{COL_TEXT}; font-size:11pt; letter-spacing:2px;")
        return l

    # ----- data -----
    @property
    def user_path(self) -> Optional[str]:
        it = self.user_list.currentItem()
        return it.data(ROLE_PATH) if it else None

    @property
    def user(self) -> Optional[tw.UserProfile]:
        u = self.users.get(self.user_path)
        return u if isinstance(u, tw.UserProfile) else None

    @property
    def workout_path(self) -> Optional[str]:
        it = self.list.currentItem()
        return it.data(ROLE_PATH) if it else None

    def refresh(self) -> None:
        self.refresh_users(select=self.user_path)

    def refresh_users(self, select: Optional[str] = None) -> None:
        self._loading = True
        self.user_list.clear()
        self.users = {}
        for path in user_files():
            try:
                u = tw.load_user(path)
                self.users[path] = u
                age = f" · age {u.age}" if u.age else ""
                text = (f"{u.display_name}\nFTP {u.ftp:.0f} W · {u.weight_kg:g} kg · "
                        f"{u.ftp / u.weight_kg:.2f} W/kg{age}")
            except tw.WorkoutError as e:
                self.users[path] = str(e)
                text = f"⚠ {os.path.basename(path)}\n{str(e).split(': ', 1)[-1]}"
            item = QtWidgets.QListWidgetItem(text)
            item.setData(ROLE_PATH, path)
            item.setSizeHint(QtCore.QSize(0, 48))
            self.user_list.addItem(item)
        row = next((i for i in range(self.user_list.count())
                    if self.user_list.item(i).data(ROLE_PATH) == select), 0)
        if self.user_list.count():
            self.user_list.setCurrentRow(row)  # signals are off (_loading): one refresh below
        self._loading = False
        self._user_changed()

    def _user_changed(self, *_):
        if self._loading:
            return
        self.refresh_workouts(select=self.workout_path or self.state.get("workout"))
        self._save_state()

    def refresh_workouts(self, select: Optional[str] = None) -> None:
        u = self.user
        ftp = u.ftp if u else 200.0
        self._loading = True
        self.list.clear()
        files = workout_files()
        for path in files:
            try:
                w = tw.load_workout(path)
                tw.WorkoutPlayer(w, ftp)  # same checks as riding it
                pm = self.cache.get(path, w, ftp)
                intensity, tss = workout_load(w, ftp)
                meta = (f"{tw.fmt_time(w.total_s)}  ·  {len(w.steps)} steps  ·  "
                        f"IF {intensity:.2f}  ·  ≈ {tss:.0f} TSS")
                row = WorkoutRow(path, pm, w.name, meta, w.description)
            except (tw.WorkoutError, OSError) as e:
                row = WorkoutRow(path, None, os.path.basename(path), str(e).split(": ", 1)[-1], "", error=str(e))
            row.ride.connect(self.ride)
            row.edit.connect(self.edit_workout)
            item = QtWidgets.QListWidgetItem()
            item.setData(ROLE_PATH, path)
            item.setSizeHint(QtCore.QSize(0, THUMB_SIZE[1] + 14))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        row = next((i for i in range(self.list.count()) if self.list.item(i).data(ROLE_PATH) == select), 0)
        self._loading = False
        if self.list.count():
            self.list.setCurrentRow(row)
            self.list.scrollToItem(self.list.currentItem(), QtWidgets.QAbstractItemView.PositionAtCenter)
        self._apply_filter()
        self._watch(files)
        who = f"{u.display_name}, FTP {u.ftp:.0f} W" if u else "no valid rider selected (pictures use 200 W)"
        self.status.setText(f"{len(files)} workouts  ·  {who}  ·  {self.cache.rendered} pictures drawn, "
                            f"the rest from cache")

    def _watch(self, files: list) -> None:
        old = self.watcher.files() + self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        paths = [p for p in (WORKOUT_DIR, USER_DIR) if os.path.isdir(p)] + files + user_files()
        self.watcher.addPaths(paths)

    def _apply_filter(self) -> None:
        q = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            row = self.list.itemWidget(it)
            text = os.path.basename(it.data(ROLE_PATH)).lower()
            if row is not None:
                text += " " + " ".join(l.text().lower() for l in row.findChildren(QtWidgets.QLabel))
            it.setHidden(bool(q) and q not in text)

    def _save_state(self) -> None:
        if self._loading:
            return
        self.state.update({"user": self.user_path, "workout": self.workout_path,
                           "hrm": self.hrm.isChecked(), "autopause": self.autopause.isChecked(),
                           "beeps": self.beeps.isChecked()})
        save_state(self.state)

    # ----- actions -----
    def _run(self, *args: str) -> None:
        subprocess.Popen([sys.executable, os.path.join(HERE, "tacx_neo.py"), *args], cwd=HERE)

    def _need_user(self) -> Optional[str]:
        if self.user is None:
            QtWidgets.QMessageBox.warning(self, "Rider", "Select a rider without errors first "
                                          "(or create one with New rider).")
            return None
        return self.user_path

    def ride(self, path: str) -> None:
        upath = self._need_user()
        if not upath:
            return
        self._select_workout(path)
        args = ["dashboard", "--workout", path, "--user", upath]
        if self.hrm.isChecked():
            args.append("--hrm")
        if not self.autopause.isChecked():
            args.append("--no-autopause")
        if not self.beeps.isChecked():
            args.append("--no-beeps")
        self._run(*args)
        self.status.setText(f"Starting {os.path.basename(path)} for {self.user.display_name}…")

    def free_ride(self) -> None:
        upath = self._need_user()
        if upath:
            self._run("dashboard", "--user", upath, *(["--hrm"] if self.hrm.isChecked() else []))

    def edit_workout(self, path: Optional[str]) -> None:
        if path:
            self._select_workout(path)
        args = ["editor"] + ([path] if path else [])
        if self.user is not None:
            args += ["--user", self.user_path]
        self._run(*args)

    def _select_workout(self, path: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(ROLE_PATH) == path:
                self.list.setCurrentRow(i)
        self._save_state()

    def new_user(self) -> None:
        dlg = RiderDialog(self)
        if dlg.exec():
            self.refresh_users(select=dlg.path)

    def edit_user(self) -> None:
        if not self.user_path:
            return
        try:
            dlg = RiderDialog(self, self.user_path)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, "Edit rider", f"Couldn't read {self.user_path}:\n{e}")
            return
        if dlg.exec():
            self.refresh_users(select=dlg.path)


def apply_launcher_theme(app: QtWidgets.QApplication) -> None:
    apply_editor_theme(app)
    app.setStyleSheet(app.styleSheet() +
                      f"QListWidget {{ background:{COL_BG}; border:1px solid #30363d; border-radius:6px; }}"
                      f"QListWidget::item {{ color:{COL_TEXT}; border-radius:6px; padding:4px; }}"
                      f"QListWidget::item:selected {{ background:#1c2d45; border:1px solid #1f6feb; }}"
                      f"QPushButton#ride {{ background:{COL_CADENCE}; color:#0d1117; font-weight:600;"
                      f" border:none; padding:6px 14px; }}"
                      f"QPushButton#ride:hover {{ background:#5fe0d4; }}"
                      f"QPushButton#ride:disabled {{ background:#30363d; color:#8b949e; }}")


def run(args=None) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_launcher_theme(app)
    win = Launcher()
    avail = app.primaryScreen().availableGeometry()
    win.resize(min(win.width(), avail.width()), min(win.height(), avail.height()))
    if args is not None and getattr(args, "screenshot", None):  # testing
        QtCore.QTimer.singleShot(1500, lambda: (win.grab().save(args.screenshot), app.quit()))
    win.show()
    app.exec()


if __name__ == "__main__":
    run()
