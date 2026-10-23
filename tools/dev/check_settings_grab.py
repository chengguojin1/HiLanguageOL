# -*- coding: utf-8 -*-
"""真实屏幕合成验证：把设置窗放在一块白底窗口上，抓真实屏幕区域。

离屏 grab() 只能证明"画了玻璃"，不能证明"透出了桌面"。这个脚本抓的是
合成后的屏幕像素：如果四角能看见白底、面板半透明，说明玻璃在真实环境生效。

运行：.venv\\Scripts\\python.exe tools\\dev\\check_settings_grab.py
输出：tools/dev/out/settings_real.png
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

from app import theme
from app.config import Config
from app.ui import SettingsDialog

OUT = Path(__file__).parent / "out"
CFG = OUT / "demo_config.ini"


def main() -> int:
    app = QApplication(sys.argv)
    theme.apply(app)
    OUT.mkdir(parents=True, exist_ok=True)
    if not CFG.exists():
        import shutil
        shutil.copy2(ROOT / "config.example.ini", CFG)
    cfg = Config(path=CFG)

    # 背景：一块纯白窗口，用来检验"能不能透出后面"
    bg = QWidget()
    pal = bg.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#FFFFFF"))
    bg.setAutoFillBackground(True)
    bg.setPalette(pal)
    bg.setGeometry(180, 120, 760, 760)
    bg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    bg.show()
    bg.raise_()

    dlg = SettingsDialog(cfg)
    dlg.move(220, 160)
    dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    dlg.show()
    dlg.raise_()

    # 强制把窗口提到前台，否则抓屏会抓到盖在上面的浏览器
    try:
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(int(dlg.winId()))
    except Exception:  # noqa: BLE001
        pass

    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)

    screen = QGuiApplication.primaryScreen()
    geo = dlg.frameGeometry()
    pm = screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
    path = OUT / "settings_real.png"
    pm.save(str(path))
    print("saved", path, pm.width(), pm.height())
    dlg.reject()
    bg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
