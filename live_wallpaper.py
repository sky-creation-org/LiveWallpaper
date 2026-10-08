"""
Live Wallpaper for Windows  (v2)
 - 画像 / GIF / 動画(MP4 など) を壁紙に設定。GIF・動画は先頭から指定秒数(初期値8秒)をループ
 - デジタル時計 + 日付(月/日/曜日)
 - タスクトレイ常駐 / 設定自動保存 / 自動起動
 - デスクトップ埋め込みは複数方式を自動で試し、画面の色を読み取って成功を確認
   (Explorer側のウィンドウ順序には一切手を加えない)。全て失敗した場合は静止画モードで動作
"""
import sys
import os
import json
import time
import calendar
import datetime
import platform
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QRect, QObject, Signal
from PySide6.QtGui import (QImage, QPixmap, QPainter, QColor, QFont, QIcon,
                           QMovie, QFontMetrics)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PySide6.QtWidgets import (QApplication, QWidget, QDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QPushButton, QComboBox,
                               QCheckBox, QSlider, QSpinBox, QFileDialog,
                               QColorDialog, QFontComboBox, QSystemTrayIcon,
                               QMenu, QLabel)

import win32api
import win32con
import win32gui

APP_NAME = "LiveWallpaper"
MUTEX_NAME = "LiveWallpaperSingleInstanceMutex"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".webm"}
FIT_MODES = [("画面を埋める (はみ出しカット)", "fill"),
             ("全体を表示 (余白あり)", "fit"),
             ("引き伸ばし", "stretch"),
             ("中央・原寸", "center")]
POSITIONS = ["左上", "上中央", "右上", "左中央", "中央", "右中央", "左下", "下中央", "右下"]

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "log.txt"
DEFAULTS = {
    "wallpaper": "",
    "fit": "fill",
    "show_clock": True,
    "show_seconds": True,
    "hour24": True,
    "lang": "ja",
    "position": 8,
    "size": 120,
    "color": "#ffffff",
    "font": "Segoe UI",
    "autostart": False,
    "loop_seconds": 8,        # GIF/動画のループ秒数 (0=全体)
    "render_mode": "auto",    # auto=ライブ表示を試す / static=静止画モード
}


# ---------------------------------------------------------------- ログ・設定
def log(msg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 200_000:
            LOG_PATH.unlink()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    if cfg.get("render_mode") not in ("auto", "static"):
        cfg["render_mode"] = "auto"
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"save_config failed: {e!r}")


def set_autostart(enabled):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    try:
        if enabled:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                exe = sys.executable
                pyw = Path(exe).with_name("pythonw.exe")
                if pyw.exists():
                    exe = str(pyw)
                cmd = f'"{exe}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ---------------------------------------------------------------- 壁紙ソース (画像/GIF/動画)
class WallpaperSource(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.movie = None
        self.paused = False
        self.animated = False
        self.loop_ms = 0              # 0 = ループ区間を制限しない
        self._gif_elapsed = 0
        self._gif_last_delay = 0
        self._last_emit = 0.0
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink()
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_video_frame)

    def _on_video_frame(self, frame):
        if not frame.isValid():
            return
        if self.loop_ms > 0:
            t = frame.startTime()  # マイクロ秒
            if t > 0 and t >= self.loop_ms * 1000:
                self.player.setPosition(0)  # 指定秒数に達したら先頭へ
                return
        now = time.monotonic()
        if now - self._last_emit < 0.03:    # 約30fpsに制限 (UIスレッドの負荷対策)
            return
        self._last_emit = now
        self.image = frame.toImage()
        self.changed.emit()

    def _stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        if self.movie:
            self.movie.stop()
            self.movie.deleteLater()
            self.movie = None
        self.image = QImage()
        self.animated = False

    def load(self, path):
        self._stop()
        if not path or not os.path.isfile(path):
            self.changed.emit()
            return
        ext = Path(path).suffix.lower()
        log(f"load wallpaper: {path}")
        if ext in IMAGE_EXT:
            self.image = QImage(path)
        elif ext == ".gif":
            self.animated = True
            self.movie = QMovie(path)
            self.movie.setCacheMode(QMovie.CacheAll)
            self._gif_elapsed = 0
            self._gif_last_delay = 0
            self.movie.frameChanged.connect(self._on_gif_frame)
            self.movie.finished.connect(self.movie.start)
            self.movie.start()
            if self.paused:
                self.movie.setPaused(True)
        elif ext in VIDEO_EXT:
            self.animated = True
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.setLoops(QMediaPlayer.Loops.Infinite)
            if self.paused:
                self.player.pause()
            else:
                self.player.play()
        self.changed.emit()

    def _on_gif_frame(self, n):
        m = self.movie
        if not m:
            return
        if n == 0:
            self._gif_elapsed = 0
        else:
            self._gif_elapsed += self._gif_last_delay
        if self.loop_ms > 0 and n != 0 and self._gif_elapsed >= self.loop_ms:
            m.jumpToFrame(0)  # 指定秒数に達したら先頭へ
            return
        self._gif_last_delay = max(m.nextFrameDelay(), 10)
        self.image = m.currentImage()
        self.changed.emit()

    def set_paused(self, paused):
        self.paused = paused
        if self.movie:
            self.movie.setPaused(paused)
        if self.player.source().isValid():
            self.player.pause() if paused else self.player.play()


# ---------------------------------------------------------------- 描画(共通)
def target_rect(w, h, iw, ih, mode):
    if mode == "stretch":
        return QRect(0, 0, w, h)
    if mode == "center":
        return QRect((w - iw) // 2, (h - ih) // 2, iw, ih)
    scale = max(w / iw, h / ih) if mode == "fill" else min(w / iw, h / ih)
    tw, th = int(iw * scale), int(ih * scale)
    return QRect((w - tw) // 2, (h - th) // 2, tw, th)


def paint_scene(p, w, h, img, cfg, smooth=True):
    p.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
    p.fillRect(0, 0, w, h, Qt.black)
    if not img.isNull():
        p.drawImage(target_rect(w, h, img.width(), img.height(), cfg["fit"]), img)
    if not cfg["show_clock"]:
        return

    now = datetime.datetime.now()
    if cfg["hour24"]:
        time_text = now.strftime("%H:%M" + (":%S" if cfg["show_seconds"] else ""))
    else:
        h12 = now.hour % 12 or 12
        time_text = f"{h12}:{now.minute:02d}" + (f":{now.second:02d}" if cfg["show_seconds"] else "")
        time_text += " AM" if now.hour < 12 else " PM"
    if cfg["lang"] == "ja":
        date_text = f"{now.month}月{now.day}日 {'月火水木金土日'[now.weekday()]}曜日"
    else:
        date_text = f"{calendar.month_name[now.month]} {now.day}  {calendar.day_name[now.weekday()]}"

    size = cfg["size"]
    f_time = QFont(cfg["font"])
    f_time.setPixelSize(size)
    f_time.setWeight(QFont.DemiBold)
    f_date = QFont(cfg["font"])
    f_date.setPixelSize(max(12, int(size * 0.38)))
    fm_t, fm_d = QFontMetrics(f_time), QFontMetrics(f_date)
    tw, dw = fm_t.horizontalAdvance(time_text), fm_d.horizontalAdvance(date_text)
    bw = max(tw, dw)
    bh = fm_t.height() + fm_d.height()

    margin = max(30, int(size * 0.5))
    col, row = cfg["position"] % 3, cfg["position"] // 3
    x = [margin, (w - bw) // 2, w - bw - margin][col]
    y = [margin, (h - bh) // 2, h - bh - margin][row]

    def line_x(lw):
        return [x, x + (bw - lw) // 2, x + bw - lw][col]

    color = QColor(cfg["color"])
    shadow = QColor(0, 0, 0, 150)
    for font, fm, text, lw, ty in (
            (f_time, fm_t, time_text, tw, y),
            (f_date, fm_d, date_text, dw, y + fm_t.height())):
        p.setFont(font)
        lx, base = line_x(lw), ty + fm.ascent()
        p.setPen(shadow)
        p.drawText(lx + 2, base + 2, text)
        p.setPen(color)
        p.drawText(lx, base, text)


# ---------------------------------------------------------------- デスクトップへの埋め込み
# 方針: 自分のウィンドウだけを動かし、Explorer側のウィンドウ(アイコン層など)には一切触れない。
#       各方式のあと画面の実ピクセルを読み取り、本当に表示されたものだけを採用する。
def direct_children(parent):
    res, h = [], 0
    while len(res) < 50:
        h = win32gui.FindWindowEx(parent, h, None, None)
        if not h:
            break
        res.append(h)
    return res


def describe(hwnd):
    try:
        cls = win32gui.GetClassName(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        return f"{hwnd}:{cls}:visible={vis}:rect={rect}"
    except Exception as e:
        return f"{hwnd}:?{e!r}"


def desktop_candidates():
    progman = win32gui.FindWindow("Progman", None)
    for wp, lp in ((0xD, 1), (0, 0)):  # WorkerW を生成させる合図 (環境によって必要な値が異なる)
        try:
            win32gui.SendMessageTimeout(progman, 0x052C, wp, lp, win32con.SMTO_NORMAL, 1000)
        except Exception as e:
            log(f"SendMessageTimeout({wp},{lp}) failed: {e!r}")

    log(f"Windows {platform.version()} / Progman {describe(progman)}")
    for c in direct_children(progman):
        log(f"  Progman child {describe(c)}")

    legacy = []

    def cb(hwnd, _):
        if win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None):
            w = win32gui.FindWindowEx(0, hwnd, "WorkerW", None)
            if w:
                legacy.append(w)
        return True

    win32gui.EnumWindows(cb, None)
    defview = win32gui.FindWindowEx(progman, 0, "SHELLDLL_DefView", None)
    child_ww = win32gui.FindWindowEx(progman, 0, "WorkerW", None)
    log(f"legacy_workerw={legacy[:1]} defview_in_progman={defview} workerw_in_progman={child_ww}")

    def cand(name, parent, after, child):
        return {"name": name, "parent": parent, "after": after, "child": child}

    c = []
    if legacy:  # Windows 10 / 11 24H2 より前
        c.append(cand("legacy-popup", legacy[0], win32con.HWND_BOTTOM, False))
        c.append(cand("legacy-child", legacy[0], win32con.HWND_BOTTOM, True))
    if defview:  # Windows 11 24H2 以降: アイコン層の真下・標準壁紙層の上
        c.append(cand("progman-child-below-icons", progman, defview, True))
        c.append(cand("progman-popup-below-icons", progman, defview, False))
    if child_ww:
        c.append(cand("workerw-child", child_ww, win32con.HWND_TOP, True))
    c.append(cand("progman-child-bottom", progman, win32con.HWND_BOTTOM, True))
    c.append(cand("progman-popup-bottom", progman, win32con.HWND_BOTTOM, False))
    return c


def apply_candidate(hwnd, cand, rect, orig_style):
    l, t, r, b = rect
    win32gui.SetParent(hwnd, cand["parent"])
    style = orig_style
    if cand["child"]:
        style = (style & ~win32con.WS_POPUP) | win32con.WS_CHILD
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
    cx, cy = win32gui.ScreenToClient(cand["parent"], (l, t))
    win32gui.SetWindowPos(hwnd, cand["after"], cx, cy, r - l, b - t,
                          win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW | win32con.SWP_FRAMECHANGED)


def looks_visible(rect):
    """画面の実際のピクセルを読み、テスト色(マゼンタ)が見えているか判定"""
    l, t, r, b = rect
    hdc = win32gui.GetDC(0)
    hits = total = 0
    try:
        for gx in range(10):
            for gy in range(6):
                x = l + int((gx + 0.5) * (r - l) / 10)
                y = t + int((gy + 0.5) * (b - t) / 6)
                c = win32gui.GetPixel(hdc, x, y)
                total += 1
                if c != -1:
                    rr, gg, bb = c & 255, (c >> 8) & 255, (c >> 16) & 255
                    if rr > 235 and gg < 25 and bb > 235:
                        hits += 1
    finally:
        win32gui.ReleaseDC(0, hdc)
    log(f"  visibility {hits}/{total}")
    return hits >= total * 0.3


class WallpaperWindow(QWidget):
    def __init__(self, ctx, phys_rect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool)
        self.ctx = ctx
        self.phys_rect = phys_rect  # (l, t, r, b) 物理ピクセル
        self.testing = True         # 埋め込みテスト中はマゼンタで塗る
        self.cands = []
        self.idx = 0
        self.hwnd = 0
        self.orig_style = 0
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.show()
        self.hwnd = int(self.winId())
        self.orig_style = win32gui.GetWindowLong(self.hwnd, win32con.GWL_STYLE)
        QTimer.singleShot(150, self.start_embed)

    def alive(self):
        return bool(self.hwnd) and win32gui.IsWindow(self.hwnd)

    def start_embed(self):
        try:
            self.cands = desktop_candidates()
        except Exception as e:
            log(f"desktop_candidates failed: {e!r}")
            self.cands = []
        self.try_next()

    def try_next(self):
        if self.idx >= len(self.cands):
            log("all embed strategies failed")
            self.testing = False
            self.ctx.embed_failed()
            return
        cand = self.cands[self.idx]
        self.idx += 1
        log(f"try {cand['name']}")
        try:
            apply_candidate(self.hwnd, cand, self.phys_rect, self.orig_style)
        except Exception as e:
            log(f"  apply failed: {e!r}")
            QTimer.singleShot(0, self.try_next)
            return
        self.update()
        QTimer.singleShot(500, lambda: self.verify(cand["name"]))

    def verify(self, name):
        try:
            ok = looks_visible(self.phys_rect)
        except Exception as e:
            log(f"  verify error: {e!r}")
            ok = False
        log(f"  -> {name}: {'OK' if ok else 'not visible'}")
        if ok:
            self.testing = False
            self.update()
            self.ctx.embed_ok(name)
        else:
            self.try_next()

    def paintEvent(self, _):
        p = QPainter(self)
        if self.testing:
            p.fillRect(self.rect(), QColor(255, 0, 255))
        else:
            paint_scene(p, self.width(), self.height(), self.ctx.source.image,
                        self.ctx.cfg, smooth=not self.ctx.source.animated)
        p.end()


# ---------------------------------------------------------------- 設定ダイアログ
class SettingsDialog(QDialog):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Live壁紙の設定")
        self.setMinimumWidth(460)
        cfg = ctx.cfg
        form = QFormLayout(self)

        self.mode_label = QLabel(f"現在の表示モード: {ctx.mode}")
        form.addRow(self.mode_label)

        self.path_edit = QLineEdit(cfg["wallpaper"])
        self.path_edit.setReadOnly(True)
        btn = QPushButton("参照...")
        btn.clicked.connect(ctx.choose_wallpaper)
        row = QHBoxLayout()
        row.addWidget(self.path_edit)
        row.addWidget(btn)
        form.addRow("壁紙 (画像/GIF/MP4)", row)

        self.fit = QComboBox()
        for label, key in FIT_MODES:
            self.fit.addItem(label, key)
        self.fit.setCurrentIndex(max(0, self.fit.findData(cfg["fit"])))
        self.fit.currentIndexChanged.connect(lambda: self.set("fit", self.fit.currentData()))
        form.addRow("表示方法", self.fit)

        self.loop = QSpinBox()
        self.loop.setRange(0, 60)
        self.loop.setSuffix(" 秒 (0=全体)")
        self.loop.setValue(cfg["loop_seconds"])
        self.loop.valueChanged.connect(lambda v: self.set("loop_seconds", v))
        form.addRow("GIF/動画のループ区間", self.loop)

        form.addRow(QLabel("<b>デジタル時計</b>"))
        form.addRow("時計を表示", self.check("show_clock"))
        form.addRow("秒を表示", self.check("show_seconds"))
        form.addRow("24時間表示", self.check("hour24"))

        self.lang = QComboBox()
        self.lang.addItem("日本語 (10月7日 水曜日)", "ja")
        self.lang.addItem("English (October 7  Wednesday)", "en")
        self.lang.setCurrentIndex(max(0, self.lang.findData(cfg["lang"])))
        self.lang.currentIndexChanged.connect(lambda: self.set("lang", self.lang.currentData()))
        form.addRow("日付の言語", self.lang)

        self.pos = QComboBox()
        self.pos.addItems(POSITIONS)
        self.pos.setCurrentIndex(cfg["position"])
        self.pos.currentIndexChanged.connect(lambda i: self.set("position", i))
        form.addRow("表示位置", self.pos)

        self.size = QSlider(Qt.Horizontal)
        self.size.setRange(40, 360)
        self.size.setValue(cfg["size"])
        self.size.valueChanged.connect(lambda v: self.set("size", v))
        form.addRow("文字サイズ", self.size)

        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(cfg["font"]))
        self.font.currentFontChanged.connect(lambda f: self.set("font", f.family()))
        form.addRow("フォント", self.font)

        self.color_btn = QPushButton()
        self.update_color_btn()
        self.color_btn.clicked.connect(self.pick_color)
        form.addRow("文字色", self.color_btn)

        form.addRow(QLabel("<b>その他</b>"))
        self.render = QComboBox()
        self.render.addItem("自動 (ライブ表示を試す・推奨)", "auto")
        self.render.addItem("静止画モード (動かないが確実)", "static")
        self.render.setCurrentIndex(max(0, self.render.findData(cfg["render_mode"])))
        self.render.currentIndexChanged.connect(
            lambda: self.ctx.change_render_mode(self.render.currentData()))
        form.addRow("デスクトップ表示方式", self.render)

        auto = self.check("autostart")
        auto.toggled.connect(set_autostart)
        form.addRow("Windows起動時に自動実行", auto)

        logbtn = QPushButton("動作ログを開く")
        logbtn.clicked.connect(ctx.open_log)
        form.addRow(logbtn)

        close = QPushButton("閉じる")
        close.clicked.connect(self.hide)
        form.addRow(close)

    def check(self, key):
        cb = QCheckBox()
        cb.setChecked(self.ctx.cfg[key])
        cb.toggled.connect(lambda v: self.set(key, v))
        return cb

    def set(self, key, value):
        self.ctx.cfg[key] = value
        self.ctx.apply()

    def update_color_btn(self):
        c = self.ctx.cfg["color"]
        self.color_btn.setText(c)
        fg = "#000" if QColor(c).lightness() > 128 else "#fff"
        self.color_btn.setStyleSheet(f"background:{c}; color:{fg}; padding:4px;")

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.ctx.cfg["color"]), self, "文字色を選択")
        if c.isValid():
            self.set("color", c.name())
            self.update_color_btn()


# ---------------------------------------------------------------- アプリ本体
class LiveWallpaperApp:
    def __init__(self, app):
        self.app = app
        self.cfg = load_config()
        self.windows = []
        self.dialog = None
        self.mode = "起動中"
        self.static_mode = False
        self.original_wallpaper = None
        self.static_n = 0
        self.settings_shown = False
        self.notified = False
        self.ticks = 0
        self.rebuilding = False

        self.source = WallpaperSource()
        self.source.loop_ms = int(self.cfg["loop_seconds"]) * 1000
        self.source.changed.connect(self.repaint_all)

        self.static_timer = QTimer()
        self.static_timer.timeout.connect(self.render_static)

        self.setup_tray()
        self.source.load(self.cfg["wallpaper"])
        if self.cfg["render_mode"] == "static":
            self.enter_static_mode()
            self.maybe_open_settings()
        else:
            self.rebuild_windows()

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)

        app.screenAdded.connect(self.schedule_rebuild)
        app.screenRemoved.connect(self.schedule_rebuild)

    # --- 状態表示
    def set_mode(self, text):
        self.mode = text
        log(f"mode: {text}")
        self.tray.setToolTip(f"Live壁紙 - {text}")
        if self.dialog:
            self.dialog.mode_label.setText(f"現在の表示モード: {text}")

    def embed_ok(self, name):
        if self.notified:
            return
        self.notified = True
        self.set_mode(f"ライブ表示 ({name})")
        self.tray.showMessage("Live Wallpaper", "壁紙の表示を開始しました。タスクトレイのアイコンから設定できます。",
                              QSystemTrayIcon.Information, 5000)
        self.maybe_open_settings()

    def embed_failed(self):
        if self.static_mode:
            return
        self.notified = True
        self.enter_static_mode()
        self.maybe_open_settings()

    def maybe_open_settings(self):
        if not self.cfg["wallpaper"] and not self.settings_shown:
            self.settings_shown = True
            self.open_settings()

    # --- 静止画モード
    def close_windows(self):
        for w in self.windows:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self.windows = []

    def enter_static_mode(self):
        self.static_mode = True
        self.close_windows()
        if not self.original_wallpaper:
            try:
                self.original_wallpaper = win32gui.SystemParametersInfo(win32con.SPI_GETDESKWALLPAPER, 260)
            except Exception:
                self.original_wallpaper = None
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, "10")
            winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
            winreg.CloseKey(key)
        except Exception as e:
            log(f"registry failed: {e!r}")
        self.set_mode("静止画モード (動画/GIFは動きません)")
        self.tray.showMessage(
            "Live Wallpaper",
            "この環境ではライブ表示ができなかったため、壁紙+時計を1分ごとに更新する静止画モードで動作します。",
            QSystemTrayIcon.Information, 8000)
        self.render_static()
        self.static_timer.start(60000)

    def leave_static_mode(self):
        self.static_mode = False
        self.static_timer.stop()
        if self.original_wallpaper:
            try:
                win32gui.SystemParametersInfo(
                    win32con.SPI_SETDESKWALLPAPER, self.original_wallpaper,
                    win32con.SPIF_UPDATEINIFILE | win32con.SPIF_SENDCHANGE)
            except Exception:
                pass
        self.original_wallpaper = None

    def render_static(self):
        if not self.static_mode:
            return
        try:
            w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            img = QImage(w, h, QImage.Format_RGB32)
            p = QPainter(img)
            cfg = dict(self.cfg)
            cfg["show_seconds"] = False
            paint_scene(p, w, h, self.source.image, cfg)
            p.end()
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.static_n += 1
            path = str(CONFIG_DIR / f"static_{self.static_n % 2}.png")
            img.save(path, "PNG")
            win32gui.SystemParametersInfo(
                win32con.SPI_SETDESKWALLPAPER, path,
                win32con.SPIF_UPDATEINIFILE | win32con.SPIF_SENDCHANGE)
        except Exception as e:
            log(f"render_static failed: {e!r}")

    # --- ウィンドウ管理
    def monitor_rects(self):
        return [rect for (_, _, rect) in win32api.EnumDisplayMonitors()]

    def rebuild_windows(self):
        if self.static_mode or self.rebuilding:
            return
        self.rebuilding = True
        try:
            self.close_windows()
            self.notified = False
            self.windows = [WallpaperWindow(self, r) for r in self.monitor_rects()]
        except Exception as e:
            log(f"rebuild_windows failed: {e!r}")
            self.embed_failed()
        finally:
            self.rebuilding = False

    def schedule_rebuild(self, *_):
        QTimer.singleShot(1500, self.rebuild_windows)

    def tick(self):
        self.repaint_all()
        self.ticks += 1
        # 約10秒ごとに生存確認 (Explorer再起動などで消えた場合に作り直す)
        if self.ticks % 20 == 0 and not self.static_mode and self.windows:
            if any((not w.testing) and (not w.alive()) for w in self.windows):
                log("wallpaper window lost -> rebuild")
                self.rebuild_windows()

    def repaint_all(self):
        if self.static_mode:
            return
        for w in self.windows:
            if not w.testing:
                w.update()

    def change_render_mode(self, value):
        self.cfg["render_mode"] = value
        save_config(self.cfg)
        if self.static_mode:
            self.leave_static_mode()
        if value == "static":
            self.enter_static_mode()
        else:
            self.rebuild_windows()

    def apply(self):
        self.source.loop_ms = int(self.cfg["loop_seconds"]) * 1000
        save_config(self.cfg)
        self.repaint_all()
        if self.static_mode:
            self.render_static()

    def choose_wallpaper(self):
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXT | VIDEO_EXT | {".gif"}))
        path, _ = QFileDialog.getOpenFileName(
            None, "壁紙を選択", self.cfg["wallpaper"] or str(Path.home() / "Pictures"),
            f"画像・動画 ({exts});;すべてのファイル (*.*)")
        if path:
            self.cfg["wallpaper"] = path
            self.source.load(path)
            if self.dialog:
                self.dialog.path_edit.setText(path)
            self.apply()
            if self.static_mode:
                QTimer.singleShot(1500, self.render_static)

    def open_settings(self):
        if not self.dialog:
            self.dialog = SettingsDialog(self)
        self.dialog.mode_label.setText(f"現在の表示モード: {self.mode}")
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def open_log(self):
        try:
            if not LOG_PATH.exists():
                log("(log opened)")
            os.startfile(str(LOG_PATH))
        except Exception as e:
            log(f"open_log failed: {e!r}")

    def toggle_pause(self):
        self.source.set_paused(not self.source.paused)
        self.pause_action.setText("再開" if self.source.paused else "一時停止")

    def quit(self):
        log("quit")
        self.source.player.stop()
        self.close_windows()
        if self.static_mode:
            self.leave_static_mode()
        self.app.quit()

    def setup_tray(self):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#2b7cff"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 14, 14)
        p.setPen(QColor("white"))
        p.setFont(QFont("Segoe UI", 22, QFont.Bold))
        p.drawText(pm.rect(), Qt.AlignCenter, "LW")
        p.end()

        self.tray = QSystemTrayIcon(QIcon(pm))
        self.tray.setToolTip("Live壁紙")
        menu = QMenu()
        menu.addAction("設定を開く", self.open_settings)
        menu.addAction("壁紙を選択...", self.choose_wallpaper)
        self.pause_action = menu.addAction("一時停止", self.toggle_pause)
        menu.addAction("動作ログを開く", self.open_log)
        menu.addSeparator()
        menu.addAction("終了", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.open_settings() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()
        self._menu = menu


def main():
    import win32event
    import winerror
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)  # noqa: F841 (保持が必要)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        sys.exit(0)  # すでに起動中

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    log("=== start v2 ===")
    ctx = LiveWallpaperApp(app)  # noqa: F841 (参照を保持)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
