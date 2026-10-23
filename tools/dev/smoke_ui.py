# -*- coding: utf-8 -*-
"""冒烟测试：玻璃窗 + 工具栏 + 几何记忆 + 设置页联动（不碰音频/网络）。

运行：.venv\\Scripts\\python.exe tools\\dev\\smoke_ui.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from app import theme
from app.asr_ws import TranscriptSegment
from app.config import Config
from app.ui import SettingsDialog, SubtitleWindow

CFG = ROOT / "tools" / "dev" / "out" / "demo_config.ini"

class FakeCtl:
    """只提供 UI 需要的控制器接口。"""

    class _State:
        value = "idle"

    def __init__(self):
        self.state = self._State()
        self.paused = False

    def can_summarize(self):
        return False

    def quit(self):
        pass


def main() -> int:
    app = QApplication(sys.argv)
    theme.apply(app)
    CFG.parent.mkdir(parents=True, exist_ok=True)
    if not CFG.exists():
        import shutil
        shutil.copy2(ROOT / "config.example.ini", CFG)
    cfg = Config(path=CFG)

    win = SubtitleWindow(cfg)
    win.set_controller(FakeCtl())
    win.update_controls()
    win.show()
    for _ in range(5):
        app.processEvents()

    # 1) 工具栏按钮 / 菜单动作都在
    for name in ("btn_start", "btn_pause", "btn_end", "btn_summary",
                 "btn_record", "btn_caption", "btn_more", "btn_hide"):
        assert hasattr(win, name), name

    # 1.2) 文本不允许鼠标选中，但链接可点
    flags = win.text_view.textInteractionFlags()
    assert not (flags & Qt.TextInteractionFlag.TextSelectableByMouse), "不应允许选中"
    assert flags & Qt.TextInteractionFlag.LinksAccessibleByMouse, "链接应可点"
    win._set_caption_mode(True)
    assert win.text_view.textInteractionFlags() == Qt.TextInteractionFlag.NoTextInteraction
    win._set_caption_mode(False)
    assert win.text_view.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse

    # 1.3) 收起：窗口隐藏但对象还活着（翻译不受影响）
    win._hide_to_tray(auto=False)
    assert not win.isVisible()
    win.show()

    # 2) 各状态下按钮不炸
    for state, paused in (("idle", False), ("running", False),
                          ("running", True), ("ending", False)):
        win._ctl.state.value = state
        win._ctl.paused = paused
        win.update_controls()
        app.processEvents()

    # 3) 几何记忆：改大小 + 位置 → 保存 → 重新读取
    win.setGeometry(320, 180, 520, 240)
    for _ in range(3):
        app.processEvents()
    win._save_geometry()
    again = Config(path=CFG)
    assert again.get_int("ui", "window_width") == 520, again.get("ui", "window_width")
    assert again.get_int("ui", "window_height") == 240, again.get("ui", "window_height")
    assert again.get_int("ui", "window_x") == 320, again.get("ui", "window_x")

    # 4) 空状态引导
    win._ctl.state.value = "idle"
    win._refresh_text()
    assert "开始翻译" in win.text_view.toHtml()

    # 5) 设置页：滑杆/字号联动预览不报错
    dlg = SettingsDialog(cfg)
    dlg.opacity.setValue(60)
    assert dlg.lbl_opacity.text() == "60%"
    dlg.font_size.setValue(13)
    dlg._refresh_preview()
    # 皮肤切换：预览立刻跟着变，保存后全局生效
    dlg.skin_cb.setCurrentIndex(1)
    assert dlg.preview._skin == "aurora"
    dlg.caption_segments.setValue(4)
    dlg._apply()
    assert theme.current_skin().key == "aurora"
    again = Config(path=CFG)
    assert again.get("ui", "skin") == "aurora"
    assert again.get_int("ui", "caption_segments") == 4

    # 6) 字幕模式：显示 N 段（每段 = 原文 + 译文）
    win._entries.clear()
    for i, txt in enumerate(("SEG-ONE", "SEG-TWO", "SEG-THREE")):
        win.push(TranscriptSegment(seq=i + 1, speaker="teacher", start_ms=0,
                                   end_ms=1, ru_text=txt, zh_text=f"译文{i}",
                                   status="translated"))
    win._caption_mode = True
    win._refresh_text()
    html_now = win.text_view.toHtml()
    for txt in ("SEG-ONE", "SEG-TWO", "SEG-THREE"):
        assert txt in html_now, txt
    assert "center" not in html_now.lower(), "字幕模式应为左对齐"

    # 双击文字回到窗口
    win._layout_text()
    app.processEvents()
    QTest.mouseDClick(win.text_view.viewport(), Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier, QPoint(60, 24))
    app.processEvents()
    assert not win._caption_mode, "双击应该退出字幕模式"

    # 7) 字幕颜色自适应：亮底黑字 / 暗底白字；也能固定
    from PyQt6.QtGui import QColor, QImage
    from app.ui import _median_luminance, _pick_caption_color

    light = QImage(64, 64, QImage.Format.Format_ARGB32)
    light.fill(QColor(245, 245, 245))
    dark = QImage(64, 64, QImage.Format.Format_ARGB32)
    dark.fill(QColor(20, 22, 30))
    assert _pick_caption_color(_median_luminance(light)).name() == "#000000"
    assert _pick_caption_color(_median_luminance(dark)).name() == "#ffffff"

    win._caption_mode = True
    cfg.set("ui", "caption_text_color", "#FF0000")
    win._refresh_caption_color()
    assert win._caption_color.name() == "#ff0000"
    win._refresh_text()
    assert "#ff0000" in win.text_view.toHtml().lower()
    win._caption_mode = False

    # 恢复 demo 配置，别把预览脚本带偏
    cfg.set("ui", "skin", "dark")
    cfg.set("ui", "caption_segments", "3")
    cfg.set("ui", "caption_text_color", "auto")
    cfg.set("ui", "font_size", "10")
    cfg.set("ui", "opacity", "85")
    theme.set_skin("dark")

    win.close()
    print("smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
