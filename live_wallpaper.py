"""
Live Wallpaper for Windows
 - 画像 (PNG/JPG/BMP/WEBP) / GIF / 動画 (MP4 など) を壁紙に設定
 - デジタル時計 + 日付(月/日/曜日) をオーバーレイ表示
 - タスクトレイ常駐、設定は自動保存、ログイン時の自動起動に対応

インストール:  pip install PySide6 pywin32
起動:          python live_wallpaper.py   (コンソール非表示は pythonw live_wallpaper.py)
exe化:         pip install pyinstaller
               pyinstaller --noconsole --onefile live_wallpaper.py
"""
import sys
import os
import json
import calendar
import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QRect, QObject, Signal
from PySide6.QtGui import (QImage, QPixmap, QPainter, QColor, QFont, QIcon,
                           QMovie, QFontMetrics, QAction, QGuiApplication)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PySide6.QtWidgets import (QApplication, QWidget, QDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QPushButton, QComboBox,
                               QCheckBox, QSlider, QSpinBox, QFileDialog, QColorDialog,
                               QFontComboBox, QSystemTrayIcon, QMenu, QLabel)

import win32api
import win32con
import win32gui

APP_NAME = "LiveWallpaper"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".webm"}
FIT_MODES = [("画面を埋める (はみ出しカット)", "fill"),
             ("全体を表示 (余白あり)", "fit"),
             ("引き伸ばし", "stretch"),
             ("中央・原寸", "center")]
POSITIONS = ["左上", "上中央", "右上", "左中央", "中央", "右中央", "左下", "下中央", "右下"]

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULTS = {
    "wallpaper": "",
    "fit": "fill",
    "show_clock": True,
    "show_seconds": True,
    "hour24": True,
    "lang": "ja",          # ja / en
    "position": 8,         # 0-8 (POSITIONS)
    "size": 120,           # 時刻の文字サイズ(px)
    "color": "#ffffff",
    "font": "Segoe UI",
    "autostart": False,
    "loop_seconds": 8,     # GIF/動画をループする秒数 (0=全体)
    "display_mode": "auto",  # auto / 1-5 (手動のライブ表示方式) / static
}


# ---------------------------------------------------------------- 設定の保存/読込
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("設定の保存に失敗:", e)


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
        self.loop_ms = 0           # 0 = ループ区間を制限しない
        self._gif_elapsed = 0
        self._gif_last_delay = 0
        # 動画: 1つのプレイヤーのフレームを全モニターで共有する
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
                self.player.setPosition(0)  # 指定秒数に達したら先頭に戻す
                return
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

    def load(self, path):
        self._stop()
        if not path or not os.path.isfile(path):
            self.changed.emit()
            return
        ext = Path(path).suffix.lower()
        if ext in IMAGE_EXT:
            self.image = QImage(path)
        elif ext == ".gif":
            self.movie = QMovie(path)
            self.movie.frameChanged.connect(self._on_gif_frame)
            self.movie.finished.connect(self.movie.start)  # 全体のループ
            self.movie.setCacheMode(QMovie.CacheAll)       # 先頭へ戻る処理を軽くする
            self._gif_elapsed = 0
            self._gif_last_delay = 0
            self.movie.start()
            if self.paused:
                self.movie.setPaused(True)
        elif ext in VIDEO_EXT:
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
            m.jumpToFrame(0)  # 指定秒数に達したら先頭に戻す
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


# ---------------------------------------------------------------- デスクトップ背面に埋め込むウィンドウ
def log(msg):
    """不具合調査用ログ (APPDATA 配下の LiveWallpaper フォルダの log.txt)"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 描画(共通)
def target_rect(w, h, iw, ih, mode):
    if mode == "stretch":
        return QRect(0, 0, w, h)
    if mode == "center":
        return QRect((w - iw) // 2, (h - ih) // 2, iw, ih)
    scale = max(w / iw, h / ih) if mode == "fill" else min(w / iw, h / ih)
    tw, th = int(iw * scale), int(ih * scale)
    return QRect((w - tw) // 2, (h - th) // 2, tw, th)


def paint_scene(p, w, h, img, cfg):
    """壁紙 + 時計を描画 (ライブ表示と静止画モードで共通)"""
    p.setRenderHint(QPainter.SmoothPixmapTransform)
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


# ---------------------------------------------------------------- デスクトップへの埋め込み (複数方式 + 自動検証)
def build_candidates():
    """試す埋め込み方式の一覧: (名前, 親ウィンドウ, Zオーダー基準, アイコン層)"""
    progman = win32gui.FindWindow("Progman", None)
    try:
        win32gui.SendMessageTimeout(progman, 0x052C, 0, 0, win32con.SMTO_NORMAL, 1000)
    except Exception:
        pass
    cands = []

    # (1) 従来型: アイコン層の背後にある WorkerW (Windows 10 / 11 24H2 より前)
    found = []

    def cb(hwnd, _):
        if win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None):
            w = win32gui.FindWindowEx(0, hwnd, "WorkerW", None)
            if w:
                found.append(w)
        return True

    win32gui.EnumWindows(cb, None)
    if found:
        cands.append(("legacy-workerw", found[0], win32con.HWND_BOTTOM, None))

    defview = win32gui.FindWindowEx(progman, 0, "SHELLDLL_DefView", None)
    child_ww = win32gui.FindWindowEx(progman, 0, "WorkerW", None)
    # (2) 24H2: Progman の子として、アイコン層のすぐ下に入れる
    if defview:
        cands.append(("progman-below-icons", progman, defview, None))
        # (3) 24H2: 一番上に置いてからアイコン層を再び最前面に戻す
        cands.append(("progman-raise-icons", progman, "raise", defview))
    # (4) 24H2: 標準壁紙の層の中に入れる
    if child_ww:
        cands.append(("progman-wallpaper-layer", child_ww, win32con.HWND_TOP, None))
    # (5) 最後の手段
    cands.append(("progman-bottom", progman, win32con.HWND_BOTTOM, None))
    return cands


def apply_candidate(hwnd, cand, rect):
    name, parent, after, defview = cand
    l, t, r, b = rect
    win32gui.SetParent(hwnd, parent)
    cx, cy = win32gui.ScreenToClient(parent, (l, t))
    insert = win32con.HWND_TOP if after == "raise" else after
    flags = win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW
    win32gui.SetWindowPos(hwnd, insert, cx, cy, r - l, b - t, flags)
    if after == "raise" and defview:
        win32gui.SetWindowPos(defview, win32con.HWND_TOP, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)


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
    log(f"visibility check: {hits}/{total}")
    return hits >= total * 0.3


class WallpaperWindow(QWidget):
    def __init__(self, ctx, phys_rect, forced=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool)
        self.ctx = ctx
        self.forced = forced  # None=自動 / '1'..'5'=手動で方式を指定
        self.phys_rect = phys_rect  # (l, t, r, b) 物理ピクセル
        self.testing = True         # 埋め込みテスト中はマゼンタで塗る
        self.cands = []
        self.idx = 0
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.show()
        QTimer.singleShot(150, self.start_embed)

    def start_embed(self):
        try:
            self.cands = build_candidates()
        except Exception as e:
            log(f"build_candidates failed: {e!r}")
            self.cands = []
        if self.forced is not None:
            i = int(self.forced) - 1
            if 0 <= i < len(self.cands):
                cand = self.cands[i]
                try:
                    apply_candidate(int(self.winId()), cand, self.phys_rect)
                    log(f"manual strategy {cand[0]} applied")
                    self.testing = False
                    self.update()
                    self.ctx.embed_ok(f"{cand[0]} / 手動")
                except Exception as e:
                    log(f"manual strategy failed: {e!r}")
                    self.testing = False
                return
            log(f"manual strategy {self.forced} unavailable -> auto")
            self.ctx.tray.showMessage("Live Wallpaper", f"方式{self.forced}はこのPCにないため、自動で選びます。",
                                      QSystemTrayIcon.Information, 4000)
            self.forced = None
        self.try_next()

    def try_next(self):
        if self.idx >= len(self.cands):
            log("all embed strategies failed")
            self.testing = False
            self.ctx.embed_failed()
            return
        cand = self.cands[self.idx]
        self.idx += 1
        try:
            apply_candidate(int(self.winId()), cand, self.phys_rect)
        except Exception as e:
            log(f"{cand[0]}: apply failed {e!r}")
            QTimer.singleShot(0, self.try_next)
            return
        self.update()
        QTimer.singleShot(500, lambda: self.verify(cand[0]))

    def verify(self, name):
        try:
            ok = looks_visible(self.phys_rect)
        except Exception as e:
            log(f"{name}: verify error {e!r}")
            ok = False
        log(f"strategy {name}: {'OK' if ok else 'not visible'}")
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
            paint_scene(p, self.width(), self.height(), self.ctx.source.image, self.ctx.cfg)
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

        # 壁紙
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

        # 時計
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
        self.disp = QComboBox()
        self.disp.addItem("自動 (推奨)", "auto")
        for i in range(1, 6):
            self.disp.addItem(f"ライブ表示 方式{i} (手動)", str(i))
        self.disp.addItem("静止画モード (動かない・確実)", "static")
        self.disp.setCurrentIndex(max(0, self.disp.findData(cfg["display_mode"])))
        self.disp.currentIndexChanged.connect(
            lambda: self.ctx.change_display_mode(self.disp.currentData()))
        form.addRow("デスクトップ表示方式", self.disp)
        auto = self.check("autostart")
        auto.toggled.connect(set_autostart)
        form.addRow("Windows起動時に自動実行", auto)

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
        self.source = WallpaperSource()
        self.source.loop_ms = int(self.cfg["loop_seconds"]) * 1000
        self.source.changed.connect(self.repaint_all)
        self.dialog = None
        self.mode = "起動中"
        self.static_mode = False
        self.original_wallpaper = None
        self.static_n = 0
        self.settings_shown = False
        self.notified = False

        self.setup_tray()
        self.source.load(self.cfg["wallpaper"])
        self.static_timer = QTimer()
        self.static_timer.timeout.connect(self.render_static)
        if self.cfg["display_mode"] == "static":
            self.enter_static_mode()
            self.maybe_open_settings()
        else:
            self.rebuild_windows()

        self.timer = QTimer()
        self.timer.timeout.connect(self.repaint_all)
        self.timer.start(500)  # 時計の更新

        app.screenAdded.connect(self.schedule_rebuild)
        app.screenRemoved.connect(self.schedule_rebuild)

    # --- 埋め込み結果
    def set_mode(self, text):
        self.mode = text
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

    # --- 静止画モード (ライブ表示ができない環境用)
    def enter_static_mode(self):
        log("enter static mode")
        self.static_mode = True
        for w in self.windows:
            w.close()
            w.deleteLater()
        self.windows = []
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
            log(f"registry failed {e!r}")
        self.set_mode("静止画モード (動画/GIFは動きません)")
        self.tray.showMessage(
            "Live Wallpaper",
            "この環境ではライブ表示ができないため、壁紙+時計を1分ごとに更新する静止画モードで動作します。",
            QSystemTrayIcon.Information, 8000)
        self.render_static()
        self.static_timer.start(60000)

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
            log(f"render_static failed {e!r}")

    # --- ウィンドウ管理
    def monitor_rects(self):
        return [rect for (_, _, rect) in win32api.EnumDisplayMonitors()]

    def rebuild_windows(self):
        if self.static_mode:
            return
        for w in self.windows:
            w.close()
            w.deleteLater()
        self.notified = False
        mode = self.cfg["display_mode"]
        forced = mode if mode not in ("auto", "static") else None
        self.windows = [WallpaperWindow(self, r, forced) for r in self.monitor_rects()]

    def schedule_rebuild(self, *_):
        QTimer.singleShot(1500, self.rebuild_windows)

    def repaint_all(self):
        if self.static_mode:
            return
        for w in self.windows:
            if not w.testing:
                w.update()

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

    def change_display_mode(self, value):
        self.cfg["display_mode"] = value
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
            if self.static_mode:  # 動画/GIFの最初のフレームが届いたあとに再描画
                QTimer.singleShot(1500, self.render_static)

    def open_settings(self):
        if not self.dialog:
            self.dialog = SettingsDialog(self)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def toggle_pause(self):
        self.source.set_paused(not self.source.paused)
        self.pause_action.setText("再開" if self.source.paused else "一時停止")

    def quit(self):
        self.source.player.stop()
        for w in self.windows:
            w.close()
        if self.static_mode and self.original_wallpaper:
            try:
                win32gui.SystemParametersInfo(
                    win32con.SPI_SETDESKWALLPAPER, self.original_wallpaper,
                    win32con.SPIF_UPDATEINIFILE | win32con.SPIF_SENDCHANGE)
            except Exception:
                pass
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
        menu.addSeparator()
        menu.addAction("終了", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.open_settings() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()
        self._menu = menu


MUTEX_NAME = "LiveWallpaperSingleInstanceMutex"  # installer.iss の AppMutex と同じ値


def main():
    import win32event
    import winerror
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)  # noqa: F841 (保持が必要)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        sys.exit(0)  # すでに起動中

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    ctx = LiveWallpaperApp(app)  # noqa: F841 (参照を保持)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
