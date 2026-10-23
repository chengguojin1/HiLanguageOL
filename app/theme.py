# -*- coding: utf-8 -*-
"""液态玻璃主题：自绘暗玻璃材质 + 深色控件样式 + 玻璃对话框基类。

设计要点
--------
* 玻璃不是"半透明黑方块"：由 4 层叠出来 ——
  着色渐变（上浅下深）→ 顶部高光（sheen）→ 细颗粒（grain）→ 折射描边（rim）。
  描边用"上亮、中间最暗、下沿回亮"的渐变，模拟玻璃边缘的折射光。
* 字幕窗是置顶逐像素透明窗口，DWM 亚克力与这种窗口冲突，
  所以这里不抓屏、不用系统模糊，纯 QPainter 自绘，零额外依赖、拖动不掉帧。
* 所有颜色集中在本模块，换配色只改常量。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------- 调色板
TEXT = QColor("#EDF1FA")            # 主文字
TEXT_DIM = QColor(226, 233, 246, 158)   # 次要文字（原文、状态）
TEXT_FAINT = QColor(200, 210, 230, 108)
HINT = "#93A0BF"                    # 设置页提示文字
ACCENT = "#8AB4FF"                  # 主色（冷蓝）
ACCENT_SOFT = "#B9A7FF"             # 次色（淡紫）
OK_GREEN = "#5BE3A6"
WARN = "#FFC46B"
DANGER = "#FF8C8C"

# 玻璃几何
RADIUS = 20          # 字幕窗圆角
BAR_RADIUS = 14      # 工具条胶囊圆角
DIALOG_RADIUS = 20


def font(pt: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """统一的界面字体：Windows 上落到 Segoe UI + 微软雅黑。"""
    f = QFont()
    f.setFamilies(["Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "Arial"])
    f.setPointSizeF(pt)
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


# ---------------------------------------------------------------- 颗粒
_GRAIN: QPixmap | None = None


def grain_tile() -> QPixmap:
    """细颗粒噪点（一次性生成后缓存）。玻璃的"材质感"主要靠它。"""
    global _GRAIN
    if _GRAIN is not None:
        return _GRAIN
    size = 96
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    rnd = random.Random(20261023)
    p = QPainter(img)
    p.setPen(Qt.PenStyle.NoPen)
    for _ in range(size * size // 5):
        v = rnd.randint(120, 255)
        p.setBrush(QColor(v, v, v, rnd.randint(8, 20)))
        p.drawPoint(rnd.randrange(size), rnd.randrange(size))
    p.end()
    _GRAIN = QPixmap.fromImage(img)
    return _GRAIN


# ---------------------------------------------------------------- 玻璃
@dataclass(frozen=True)
class Skin:
    """一套玻璃皮肤：着色、彩色辉光、高光强度、边缘折射色、圆角。"""

    key: str
    label: str
    tint: tuple[tuple[float, QColor], ...]          # 基础着色（alpha 由面板不透明度决定）
    rim_top: QColor
    rim_mid: QColor
    rim_low: QColor
    sheen_alpha: int = 26
    radius: float = RADIUS
    gloss: tuple[tuple[float, QColor], ...] = ()    # 额外彩色辉光（极光用）
    chroma: tuple[QColor, QColor] | None = None     # 边缘色散（青 / 粉）


DARK_SKIN = Skin(
    key="dark",
    label="深色玻璃",
    tint=((0.0, QColor(26, 32, 50)), (0.42, QColor(14, 19, 32)), (1.0, QColor(8, 11, 20))),
    rim_top=QColor(255, 255, 255, 170),
    rim_mid=QColor(255, 255, 255, 42),
    rim_low=QColor(255, 255, 255, 92),
    sheen_alpha=26,
    radius=RADIUS,
)

AURORA_SKIN = Skin(
    key="aurora",
    label="极光玻璃",
    tint=((0.0, QColor(28, 44, 78)), (0.46, QColor(24, 26, 60)), (1.0, QColor(26, 16, 50))),
    rim_top=QColor(186, 240, 255, 215),
    rim_mid=QColor(196, 180, 255, 52),
    rim_low=QColor(255, 150, 210, 140),
    sheen_alpha=40,
    radius=26,
    gloss=((0.0, QColor(96, 214, 255, 120)),
           (0.48, QColor(146, 112, 255, 100)),
           (1.0, QColor(255, 108, 190, 92))),
    chroma=(QColor(110, 220, 255, 150), QColor(255, 120, 200, 135)),
)

SKINS: dict[str, Skin] = {s.key: s for s in (DARK_SKIN, AURORA_SKIN)}
_CURRENT = "dark"


def set_skin(key: str) -> None:
    """切换当前皮肤（只影响绘制，控件配色不变）。"""
    global _CURRENT
    _CURRENT = key if key in SKINS else "dark"


def current_skin() -> Skin:
    return SKINS.get(_CURRENT, DARK_SKIN)


def paint_glass(
    painter: QPainter,
    rect: QRectF,
    radius: float | None = None,
    *,
    alpha: float = 0.88,
    shadow: int = 0,
    sheen: bool = True,
    hover: bool = False,
    skin: Skin | None = None,
) -> None:
    """把一块玻璃画到 painter 的 rect 上。

    alpha  0..1，玻璃整体的不透明度（面板越不透，字幕越清楚）
    shadow 0..255，外投影强度；0 = 不画（贴边浮窗不需要）
    hover  鼠标在窗口边缘时的高亮描边（提示可缩放）
    skin   用哪套皮肤，默认当前全局皮肤
    """
    sk = skin or current_skin()
    radius = sk.radius if radius is None else radius
    alpha = max(0.0, min(1.0, alpha))
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    if shadow > 0:
        painter.setPen(Qt.PenStyle.NoPen)
        steps = 10
        for i in range(steps, 0, -1):
            grow = i * 2.4
            a = int(shadow * 0.055 * (1.0 - i / (steps + 1.0)))
            painter.setBrush(QColor(0, 0, 0, a))
            painter.drawRoundedRect(
                rect.adjusted(-grow, -grow + 6, grow, grow + 9),
                radius + grow, radius + grow)

    painter.setClipPath(path)

    # 1) 着色：上浅下深，模拟玻璃厚度
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    for pos, col in sk.tint:
        c = QColor(col)
        c.setAlpha(int(255 * alpha))
        g.setColorAt(pos, c)
    painter.fillRect(rect, g)

    # 1.5) 彩色辉光（极光皮肤）：斜向的一条色带
    if sk.gloss:
        og = QLinearGradient(rect.topLeft(), rect.bottomRight())
        for pos, col in sk.gloss:
            c = QColor(col)
            c.setAlpha(int(col.alpha() * alpha))
            og.setColorAt(pos, c)
        painter.fillRect(rect, og)

    # 2) 顶部高光：一条从顶部往下衰减的柔光
    if sheen:
        s = QLinearGradient(
            rect.topLeft(),
            QPointF(rect.left(), rect.top() + rect.height() * 0.55))
        s.setColorAt(0.0, QColor(255, 255, 255, int(sk.sheen_alpha * alpha)))
        s.setColorAt(0.35, QColor(255, 255, 255, int(8 * alpha)))
        s.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, s)
        # 左上角的一点镜面反光
        glow = QRadialGradient(
            QPointF(rect.left() + rect.width() * 0.16, rect.top() + 2),
            max(90.0, rect.width() * 0.34))
        glow.setColorAt(0.0, QColor(255, 255, 255, int(22 * alpha)))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, glow)
        # 斜向一道细反光：玻璃的"曲面"感主要来自这条
        streak = QLinearGradient(
            rect.topLeft(),
            QPointF(rect.left() + rect.width() * 0.55,
                    rect.top() + rect.height() * 1.15))
        streak.setColorAt(0.0, QColor(255, 255, 255, 0))
        streak.setColorAt(0.44, QColor(255, 255, 255, 0))
        streak.setColorAt(0.50, QColor(255, 255, 255, int(13 * alpha)))
        streak.setColorAt(0.56, QColor(255, 255, 255, 0))
        streak.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, streak)

    # 3) 颗粒
    painter.setOpacity(1.0)
    painter.drawTiledPixmap(rect, grain_tile())
    painter.restore()

    # 4) 内沿折射描边：上亮 → 中间暗 → 下沿回亮
    rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    for pos, col in ((0.0, sk.rim_top), (0.28, sk.rim_mid),
                     (0.72, sk.rim_mid), (1.0, sk.rim_low)):
        c = QColor(col)
        c.setAlpha(int(col.alpha() * alpha))
        rim.setColorAt(pos, c)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(rim, 1.3))
    painter.drawRoundedRect(rect.adjusted(0.65, 0.65, -0.65, -0.65), radius, radius)

    # 4.5) 边缘色散：一道青、一道粉，极光玻璃的"厚度"就靠它
    if sk.chroma:
        painter.save()
        painter.setOpacity(0.65 * alpha + 0.2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(sk.chroma[0], 1.0))
        painter.drawRoundedRect(rect.adjusted(2.2, 2.2, -2.2, -2.2), radius - 2, radius - 2)
        painter.setPen(QPen(sk.chroma[1], 1.0))
        painter.drawRoundedRect(rect.adjusted(3.4, 3.4, -3.4, -3.4), radius - 3, radius - 3)
        painter.restore()

    # 5) 外侧暗描边：压在亮背景上也能分出轮廓
    painter.setPen(QPen(QColor(0, 0, 0, 80), 1.0))
    painter.drawRoundedRect(rect.adjusted(0.2, 0.2, -0.2, -0.2), radius, radius)

    if hover:
        painter.setPen(QPen(QColor(138, 180, 255, 210), 2.0))
        painter.drawRoundedRect(rect.adjusted(1.4, 1.4, -1.4, -1.4), radius - 1, radius - 1)


def paint_chip(
    painter: QPainter,
    rect: QRectF,
    rate: float = 1.0,
    *,
    fill: QColor | None = None,
    stroke: QColor | None = None,
) -> None:
    """小胶囊：状态角标、语言标、工具条分组背景。"""
    r = rect.height() / 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill or QColor(255, 255, 255, int(22 * rate)))
    painter.drawRoundedRect(rect, r, r)
    if stroke is not None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(stroke, 1.0))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), r, r)
    painter.restore()


# ---------------------------------------------------------------- 玻璃对话框
class GlassDialog(QDialog):
    """无边框玻璃对话框：自绘背景 + 可拖动标题栏 + 关闭按钮。

    子类往 self.body 里塞内容即可；不用管背景、圆角、拖拽。
    """

    SHADOW = 18       # 留给外投影的边距
    PAD = 20          # 内容内边距
    RADIUS = DIALOG_RADIUS
    HEADER_H = 54

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self._drag_from = None
        self._title = title

        root = QVBoxLayout(self)
        m = self.SHADOW
        root.setContentsMargins(m, m, m, m)
        root.setSpacing(0)

        head = QWidget()
        head.setFixedHeight(self.HEADER_H)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(self.PAD + 2, 14, self.PAD - 4, 2)
        hl.setSpacing(10)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("glassTitle")
        self.sub_label = QLabel("")
        self.sub_label.setObjectName("glassSub")
        btn_close = QPushButton("×")
        btn_close.setObjectName("glassClose")
        btn_close.setFixedSize(32, 32)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setToolTip("关闭（Esc）")
        btn_close.clicked.connect(self.reject)
        hl.addWidget(self.title_label)
        hl.addWidget(self.sub_label)
        hl.addStretch(1)
        hl.addWidget(btn_close)
        root.addWidget(head)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(self.PAD, 2, self.PAD, self.PAD)
        self.body.setSpacing(12)
        root.addLayout(self.body, 1)

    # -- 小 API --
    def set_subtitle(self, text: str) -> None:
        self.sub_label.setText(text)

    def panel_rect(self) -> QRectF:
        m = self.SHADOW
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    # -- 绘制 --
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        # 对话框面积大、底下多半是深色桌面，普通玻璃会显得像一块纯色板；
        # 这里单独加强顶部高光与上沿描边，让"玻璃"看得出来。
        base = current_skin()
        sk = replace(base,
                     sheen_alpha=max(46, base.sheen_alpha),
                     rim_top=QColor(255, 255, 255, 215))
        paint_glass(p, self.panel_rect(), self.RADIUS, alpha=0.80, shadow=190, skin=sk)
        p.end()


class GlassMenu(QMenu):
    """玻璃质感的弹出菜单（工具条 ⋯ / 托盘右键）。

    QMenu 是系统绘制的弹出窗口，只写 QSS 只能改颜色。这里把窗口设成透明，
    自己画一层玻璃，再让 QMenu 把菜单项画在上面。
    """

    RADIUS = 12

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        self.setStyleSheet(
            "QMenu{background:transparent;border:none;padding:7px;}"
            "QMenu::item{padding:7px 20px;border-radius:7px;color:#E9EEF9;}"
            "QMenu::item:selected{background:rgba(138,180,255,0.22);}"
            "QMenu::item:disabled{color:rgba(233,238,249,0.35);}"
            "QMenu::separator{height:1px;background:rgba(255,255,255,0.10);"
            "margin:5px 10px;}"
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        base = current_skin()
        sk = replace(base,
                     sheen_alpha=max(40, base.sheen_alpha),
                     rim_top=QColor(255, 255, 255, 200))
        paint_glass(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                    self.RADIUS, alpha=0.90, skin=sk)
        p.end()
        super().paintEvent(event)

    # -- 拖动标题栏 --
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and event.position().y() < self.SHADOW + self.HEADER_H):
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_from = None
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------- 控件样式
def _make_arrows() -> dict[str, str]:
    """生成下拉/微调箭头图标（QSS 只能引用图片文件，不能画三角形）。"""
    from pathlib import Path
    from tempfile import gettempdir

    from PyQt6.QtGui import QPolygonF

    base = Path(gettempdir()) / "hilang_theme"
    base.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name, up in (("down", False), ("up", True)):
        img = QImage(16, 10, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(206, 216, 238, 225), 1.6, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        pts = ([QPolygonF([QPointF(3, 7), QPointF(8, 2.5), QPointF(13, 7)])]
               if up else
               [QPolygonF([QPointF(3, 3), QPointF(8, 7.5), QPointF(13, 3)])])
        p.drawPolyline(*pts)
        p.end()
        path = base / f"chevron_{name}.png"
        img.save(str(path))
        if path.exists():
            out[name] = path.as_posix()
    return out


def qss(arrows: dict[str, str] | None = None) -> str:
    """全局深色样式。只改颜色/圆角/边框，不给通用 QWidget 设背景。"""
    arrows = arrows or {}
    arrow_css = ""
    if "down" in arrows:
        arrow_css += (f'QComboBox::down-arrow{{image:url({arrows["down"]});'
                      f'width:12px;height:10px;}}')
    if "down" in arrows and "up" in arrows:
        arrow_css += (f'QSpinBox::up-arrow{{image:url({arrows["up"]});'
                      f'width:9px;height:8px;}}'
                      f'QSpinBox::down-arrow{{image:url({arrows["down"]});'
                      f'width:9px;height:8px;}}')
    return f"""
    QWidget {{
        color: #E9EEF9;
        font-family: "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei";
        font-size: 12px;
    }}
    QLabel {{ background: transparent; }}
    QLabel#glassTitle {{ font-size: 15px; font-weight: 600; color: #F4F7FE; }}
    QLabel#glassSub {{ font-size: 11px; color: rgba(233,238,249,0.5); margin-left: 2px; }}
    QLabel#hint {{ color: {HINT}; font-size: 11px; }}
    QLabel#group {{ color: {ACCENT}; font-size: 11.5px; font-weight: 600; }}
    QLabel#value {{ color: rgba(233,238,249,0.72); font-size: 11px; }}

    QPushButton#glassClose {{
        background: rgba(255,255,255,0.10); border: none; border-radius: 16px;
        color: #E9EEF9; font-size: 20px; padding-bottom: 3px;
    }}
    QPushButton#glassClose:hover {{ background: #E05C5C; color: #FFFFFF; }}

    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabWidget::tab-bar {{ alignment: left; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{
        background: transparent; color: rgba(233,238,249,0.55);
        padding: 7px 15px; margin-right: 4px; border-radius: 10px; font-size: 12px;
    }}
    QTabBar::tab:hover {{ color: #E9EEF9; background: rgba(255,255,255,0.06); }}
    QTabBar::tab:selected {{ color: #0B1020; background: #C9D8FF; font-weight: 600; }}

    QLineEdit, QSpinBox, QComboBox {{
        background: rgba(255,255,255,0.055);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 9px; padding: 6px 9px; color: #EDF1FA;
        selection-background-color: #7FA9FF; selection-color: #08111F;
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
        border-color: rgba(138,180,255,0.8); background: rgba(255,255,255,0.085);
    }}
    QLineEdit:disabled {{ color: rgba(233,238,249,0.35); }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    {arrow_css}
    QComboBox QAbstractItemView {{
        background: #131A2B; border: 1px solid rgba(255,255,255,0.12);
        border-radius: 8px; color: #EDF1FA; padding: 4px;
        selection-background-color: rgba(138,180,255,0.26); selection-color: #FFFFFF;
        outline: none;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        width: 18px; border: none; background: transparent;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
        background: rgba(255,255,255,0.10); border-radius: 4px;
    }}

    QCheckBox {{ spacing: 8px; color: #DCE4F2; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 5px;
        border: 1px solid rgba(255,255,255,0.30); background: rgba(255,255,255,0.04);
    }}
    QCheckBox::indicator:hover {{ border-color: rgba(138,180,255,0.85); }}
    QCheckBox::indicator:checked {{ background: #8AB4FF; border-color: #8AB4FF; }}

    QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: rgba(255,255,255,0.13); }}
    QSlider::sub-page:horizontal {{ background: #8AB4FF; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
        background: #FFFFFF; border: 1px solid rgba(0,0,0,0.2);
    }}

    QPushButton {{
        background: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 10px; padding: 7px 14px; color: #E9EEF9;
    }}
    QPushButton:hover {{ background: rgba(255,255,255,0.13); }}
    QPushButton:pressed {{ background: rgba(255,255,255,0.05); }}
    QPushButton:disabled {{ color: rgba(233,238,249,0.3); background: rgba(255,255,255,0.03); }}
    QPushButton#primary {{ background: #8AB4FF; color: #08111F; font-weight: 600; border: none; }}
    QPushButton#primary:hover {{ background: #A8C7FF; }}
    QPushButton#primary:disabled {{ background: rgba(138,180,255,0.35); color: rgba(8,17,31,0.55); }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: rgba(255,255,255,0.16); border-radius: 4px; min-height: 26px;
    }}
    QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.26); }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ height: 9px; background: transparent; }}
    QScrollBar::handle:horizontal {{ background: rgba(255,255,255,0.16); border-radius: 4px; min-width: 26px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

    QMenu {{
        background: #131A2B; border: 1px solid rgba(255,255,255,0.12);
        border-radius: 10px; padding: 6px; color: #E9EEF9;
    }}
    QMenu::item {{ padding: 7px 18px; border-radius: 7px; }}
    QMenu::item:selected {{ background: rgba(138,180,255,0.22); }}
    QMenu::separator {{ height: 1px; background: rgba(255,255,255,0.08); margin: 5px 8px; }}

    QToolTip {{
        background: #0E1322; color: #E9EEF9;
        border: 1px solid rgba(255,255,255,0.14); padding: 5px 8px;
    }}
    QMessageBox {{ background: #141A2A; }}
    QMessageBox QLabel {{ color: #E9EEF9; }}
    QTextEdit, QPlainTextEdit {{
        background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
        border-radius: 9px; color: #E9EEF9;
        selection-background-color: #7FA9FF; selection-color: #08111F;
    }}
    """


def apply(app) -> None:
    """启动时调用：Fusion 风格 + 深色调色板 + 全局样式。"""
    from PyQt6.QtGui import QPalette

    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#0F1424"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#E9EEF9"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#121828"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#171E31"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#E9EEF9"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#1A2033"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#E9EEF9"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#3E6BD0"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#131A2B"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#E9EEF9"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6F7C9B"))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    app.setPalette(pal)
    try:
        arrows = _make_arrows()
    except Exception:  # noqa: BLE001 - 图标生成失败不影响主题可用
        arrows = {}
    app.setStyleSheet(qss(arrows))
