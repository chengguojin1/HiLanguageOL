# -*- coding: utf-8 -*-
"""新 UI 预览：直接实例化真实控件（SubtitleWindow / SettingsDialog）截图。

只读：用 demo 配置副本，不改 config.ini，不发任何网络请求。
运行：.venv\\Scripts\\python.exe tools\\dev\\preview_app.py
输出：tools\\dev\\out\\ui_*.png
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QRadialGradient
from PyQt6.QtWidgets import QApplication, QTabWidget

from app import theme
from app.asr_ws import TranscriptSegment
from app.config import Config
from app.ui import SettingsDialog, SubtitleWindow

OUT = ROOT / "tools" / "dev" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def wallpaper(w: int, h: int, dark: bool = False) -> QImage:
    """模拟课堂画面：亮底 PPT / 暗色视频，检验玻璃与字幕的可读性。"""
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, w, h)
    if dark:
        g.setColorAt(0.0, QColor(18, 22, 34))
        g.setColorAt(0.55, QColor(26, 32, 48))
        g.setColorAt(1.0, QColor(38, 28, 44))
    else:
        g.setColorAt(0.0, QColor(240, 243, 250))
        g.setColorAt(0.55, QColor(214, 226, 246))
        g.setColorAt(1.0, QColor(196, 208, 236))
    p.fillRect(0, 0, w, h, g)
    p.setPen(Qt.PenStyle.NoPen)
    for cx, cy, r, col in [
        (0.16, 0.20, 0.30, QColor(120, 160, 255, 150 if not dark else 90)),
        (0.80, 0.16, 0.26, QColor(255, 150, 200, 130 if not dark else 80)),
        (0.62, 0.92, 0.34, QColor(120, 226, 210, 120 if not dark else 70)),
    ]:
        rg = QRadialGradient(QPointF(cx * w, cy * h), r * min(w, h))
        rg.setColorAt(0.0, col)
        c = QColor(col)
        c.setAlpha(0)
        rg.setColorAt(1.0, c)
        p.setBrush(rg)
        p.drawEllipse(QPointF(cx * w, cy * h), r * min(w, h), r * min(w, h))
    p.setPen(QColor(230, 236, 250, 140) if dark else QColor(40, 60, 100, 90))
    p.setFont(theme.font(30, QFont.Weight.DemiBold))
    p.drawText(QRectF(40, 40, w - 80, 60),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
               "Лекция 7 · Производная функции")
    p.end()
    return img


def demo_cfg() -> Config:
    cfg_path = OUT / "demo_config.ini"
    if not cfg_path.exists():
        shutil.copy2(ROOT / "config.example.ini", cfg_path)
    return Config(path=cfg_path)


def fill(w: SubtitleWindow) -> None:
    rows = [
        (1, "teacher", "Итак, производная функции — это скорость изменения "
                       "функции в данной точке.", "所以，函数的导数就是函数在该点的变化率。"),
        (2, "me", "А если функция не дифференцируема?", "那如果函数不可导呢？"),
        (3, "teacher", "Тогда говорят, что производной в этой точке не существует.",
         "那我们就说，该点处的导数不存在。"),
    ]
    for seq, spk, ru, zh in rows:
        w.push(TranscriptSegment(seq=seq, speaker=spk, start_ms=seq * 1000,
                                 end_ms=seq * 1000 + 900, ru_text=ru,
                                 zh_text=zh, status="translated"))
    w.set_status("翻译中 · 已收到 3 句")
    w.set_channels("老师 有声音 · 我 有声音")


def compose(shot, w: int, h: int, name: str, offset=(90, 120), dark: bool = False) -> Path:
    """按物理像素合成，避免 125%/150% 缩放下把控件画糊、画裁。"""
    dpr = shot.devicePixelRatio() or 1.0
    shot.setDevicePixelRatio(1.0)   # 按物理像素 1:1 贴，避免二次缩放糊掉
    W, H = int(w * dpr), int(h * dpr)
    bg = wallpaper(W, H, dark=dark)
    p = QPainter(bg)
    p.drawPixmap(int(offset[0] * dpr), int(offset[1] * dpr), shot)
    p.end()
    path = OUT / name
    bg.save(str(path))
    return path


def main() -> None:
    app = QApplication(sys.argv)
    theme.apply(app)
    cfg = demo_cfg()

    win = SubtitleWindow(cfg)
    win.resize(560, 260)
    fill(win)
    win.btn_start.setText("翻译中…")
    win.btn_start.setEnabled(False)
    for b in (win.btn_pause, win.btn_end, win.btn_summary, win.btn_record, win.btn_caption):
        b.setEnabled(True)
    win.show()
    for _ in range(5):
        app.processEvents()
    compose(win.grab(), 820, 460, "ui_subtitle.png")

    win.toolbar.show()
    win._layout_text()
    for _ in range(5):
        app.processEvents()
    compose(win.grab(), 820, 460, "ui_subtitle_hover.png", offset=(90, 130))

    # 极光皮肤
    theme.set_skin("aurora")
    win.update()
    for _ in range(4):
        app.processEvents()
    compose(win.grab(), 820, 460, "ui_subtitle_aurora.png", offset=(90, 130))

    # 字幕模式：3 段（一段 = 原文 + 译文）
    win._caption_mode = True
    win.toolbar.hide()
    win.resize(560, 300)
    win._layout_text()
    # 1) 暗色画面 → 自动选白字
    win._caption_color = QColor(255, 255, 255)
    win._refresh_text()
    win.update()
    for _ in range(5):
        app.processEvents()
    compose(win.grab(), 820, 460, "ui_caption.png", offset=(90, 100), dark=True)

    # 2) 亮色 PPT → 自动选黑字
    win._caption_color = QColor(0, 0, 0)
    win._refresh_text()
    win.update()
    for _ in range(5):
        app.processEvents()
    compose(win.grab(), 820, 460, "ui_caption_light.png", offset=(90, 100))

    win._caption_mode = False
    win.resize(560, 260)
    win._layout_text()
    theme.set_skin("dark")
    win.update()

    # 最小宽度下工具栏会不会挤爆
    win.resize(360, 180)
    for _ in range(4):
        app.processEvents()
    compose(win.grab(), 620, 320, "ui_narrow.png", offset=(110, 90))

    win._mini_mode = True
    win.setMinimumSize(0, 0)
    win.resize(46, 46)
    win.text_view.hide()
    win.toolbar.hide()
    win.update()
    for _ in range(4):
        app.processEvents()
    compose(win.grab(), 300, 200, "ui_mini.png", offset=(120, 70))

    # 空窗口引导（第一次打开时的样子）
    empty = SubtitleWindow(cfg)
    empty.resize(560, 200)
    empty.show()
    for _ in range(5):
        app.processEvents()
    compose(empty.grab(), 820, 400, "ui_empty.png", offset=(120, 100))

    dlg = SettingsDialog(cfg)
    dlg.resize(660, 620)
    dlg.show()
    for _ in range(6):
        app.processEvents()
    compose(dlg.grab(), 820, 820, "ui_settings.png", offset=(90, 60))

    # 设备页 / AI 页：验证下拉框、滚动区、分组小标题
    tabs = dlg.findChild(QTabWidget)
    for idx, name in ((1, "ui_settings_device.png"), (4, "ui_settings_ai.png")):
        tabs.setCurrentIndex(idx)
        for _ in range(5):
            app.processEvents()
        compose(dlg.grab(), 820, 820, name, offset=(80, 50))
    print("saved", OUT)


if __name__ == "__main__":
    main()
