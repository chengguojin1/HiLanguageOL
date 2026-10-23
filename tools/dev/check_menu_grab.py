# -*- coding: utf-8 -*-
"""验证 ⋯ 玻璃菜单：弹出后抓取菜单自身像素，合成到彩色背景上看效果。

运行：.venv\\Scripts\\python.exe tools\\dev\\check_menu_grab.py
输出：tools/dev/out/menu_real.png
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient
from PyQt6.QtWidgets import QApplication

from app import theme

OUT = Path(__file__).parent / "out"


def make_bg(w: int, h: int) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0.0, QColor("#1B2440"))
    g.setColorAt(1.0, QColor("#3A2350"))
    p.fillRect(0, 0, w, h, g)
    for cx, cy, r, col in ((0.2, 0.2, 0.5, QColor(90, 140, 255, 160)),
                           (0.85, 0.8, 0.5, QColor(255, 120, 190, 140))):
        rg = QRadialGradient(cx * w, cy * h, r * min(w, h))
        rg.setColorAt(0.0, col)
        c = QColor(col)
        c.setAlpha(0)
        rg.setColorAt(1.0, c)
        p.setBrush(rg)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx * w, cy * h), r * min(w, h), r * min(w, h))
    p.end()
    return img


def main() -> int:
    app = QApplication(sys.argv)
    theme.apply(app)
    OUT.mkdir(parents=True, exist_ok=True)

    menu = theme.GlassMenu()
    menu.addAction("打开纪要目录")
    menu.addAction("打开配置目录（API Key 在这里）")
    menu.addSeparator()
    menu.addAction("设置…")
    menu.addSeparator()
    menu.addAction("退出程序")
    menu.popup(QPoint(60, 60))
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)

    pm = menu.grab()
    dpr = pm.devicePixelRatio() or 1.0
    pm.setDevicePixelRatio(1.0)
    W, H = int(360 * dpr), int(260 * dpr)
    bg = make_bg(W, H)
    p = QPainter(bg)
    p.drawPixmap(int(50 * dpr), int(40 * dpr), pm)
    p.end()
    path = OUT / "menu_real.png"
    bg.save(str(path))
    print("saved", path, pm.width(), pm.height())
    menu.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
