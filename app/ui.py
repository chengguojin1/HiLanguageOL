# -*- coding: utf-8 -*-
"""M5 字幕窗 UI：PyQt6 置顶透明双语窗（单窗口模式）。

- SubtitleWindow：无边框、置顶、暗色液态玻璃面板、可拖动+右下角缩放；
  鼠标移入浮现玻璃工具条（常用操作 + 「⋯」溢出菜单），移出只留字幕；
- SettingsDialog：玻璃对话框，显示（字号/不透明度/颜色/实时预览）、设备、
  输出、语言、AI 模型 五页，确定后写回 ini。
"""

from __future__ import annotations

import html
import queue
import time

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction, QColor, QFont, QGuiApplication, QIcon, QImage, QKeySequence,
    QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient, QTextOption,
)
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QMenu, QScrollArea, QSystemTrayIcon,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSlider, QSpinBox, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import theme
from .asr_ws import TranscriptSegment


def app_icon() -> QIcon:
    """应用图标：优先用 app/icon/app.ico（打包后从资源目录取），缺失时返回空图标。"""
    try:
        from .config import resource_path

        path = resource_path("app/icon/app.ico")
        if path.exists():
            return QIcon(str(path))
    except Exception:  # noqa: BLE001
        pass
    return QIcon()

_EDGE = 20  # 边缘缩放热区（像素），增大方便点中
_TOOLBAR_H = 40
_BAR_MARGIN = 8      # 工具条离窗口边缘的距离
_FOOTER_H = 34       # 底部拖动/状态条高度（明确的"抓这里"区域）
# 字幕模式旧段落的"淡出"基准色：没有底衬，往浅灰淡最不突兀
_CAPTION_FADE_BG = (150, 156, 168)
_FONT_MIN, _FONT_MAX = 5, 15
_AUTO_DEVICE_LABEL = "自动（跟随系统默认设备）"

# 字幕模式（全屏纯字幕）的鼠标手感参数
_CAPTION_DRAG_PX = 4        # 位移超过这么多像素才算"拖动"，避免手抖让窗口跳动、双击落空
_CAPTION_DBLCLICK_MS = 450  # 自定义双击判定窗口（比系统默认更宽松，双击更容易触发）
_CAPTION_DBLCLICK_PX = 14   # 两次点击的允许位移


def _fade_color(color: str, fade: float, bg: tuple[int, int, int] = (16, 20, 36)) -> str:
    """把颜色向底色调"淡出"：旧字幕用，Qt 富文本对 alpha 支持不稳，用混色更保险。"""
    c = QColor(color)
    if not c.isValid():
        return color
    f = max(0.0, min(1.0, fade))
    return QColor(int(c.red() * f + bg[0] * (1 - f)),
                  int(c.green() * f + bg[1] * (1 - f)),
                  int(c.blue() * f + bg[2] * (1 - f))).name()


def _median_luminance(img: QImage) -> float:
    """图片的中位亮度（0~255）；中位数能避开少量文字像素的干扰。抓不到返回 -1。"""
    if img.isNull():
        return -1.0
    small = img.scaled(64, 64, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.FastTransformation)
    vals = []
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixelColor(x, y)
            vals.append(0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue())
    vals.sort()
    return vals[len(vals) // 2]


def _pick_caption_color(lum: float) -> QColor:
    """亮底用黑字、暗底用白字；抓不到底图（lum<0）时按白字处理。"""
    return QColor(0, 0, 0) if lum >= 150 else QColor(255, 255, 255)


def _minus_icon(color: QColor | None = None) -> QIcon:
    """工具栏的「−」：自己画一条线，比字体里的减号粗、居中、跨字体稳定。"""
    c = color or QColor(236, 241, 251, 200)
    pm = QPixmap(18, 18)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(c, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(5, 9, 13, 9)
    p.end()
    return QIcon(pm)


def _group_label(text: str) -> QLabel:
    """设置页里的分组小标题。"""
    lbl = QLabel(text)
    lbl.setObjectName("group")
    return lbl


def _hint(text: str) -> QLabel:
    """设置页里的灰色说明文字（可选换行）。"""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setObjectName("hint")
    return lbl


def _sep() -> QFrame:
    """工具条里的竖分隔线。"""
    f = QFrame()
    f.setFixedWidth(1)
    f.setStyleSheet("background:rgba(255,255,255,0.10);border:none;margin:6px 2px;")
    return f


class _GlassBar(QWidget):
    """工具条底板：一条独立的小玻璃胶囊。"""

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        rect = QRectF(self.rect())
        theme.paint_glass(p, rect, theme.BAR_RADIUS, alpha=0.58)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 16))
        p.drawRoundedRect(rect, theme.BAR_RADIUS, theme.BAR_RADIUS)
        p.end()


class SubtitleWindow(QWidget):
    """置顶透明双语字幕窗（可拖动/缩放，悬停显示功能条）。"""

    def __init__(self, cfg):
        super().__init__()
        self._cfg = cfg
        self._entries: list[TranscriptSegment] = []
        self._preview_entry: TranscriptSegment | None = None  # 临时字幕（中间识别结果）
        self._user_scrolled = False  # 用户是否向上滚动锁定阅读位置
        self._pending_count = 0  # 锁定期间的新消息数量
        self._status = "就绪"
        self._channels_text = ""  # 各通道健康状态（老师/我 是否有声音）
        self._caption_mode = False
        self._caption_color = QColor(255, 255, 255)   # 字幕模式当前文字色（自动/固定）
        self._mini_mode = False
        self._normal_geom = None
        self._ctl = None
        self._drag_pos = None
        self._press_geom = None
        self._mode: str | None = None  # "move" | "left/right/top/bottom/tl/tr/bl/br"
        self._hover_edge: str | None = None
        self._press_global = None      # 字幕模式：按下点（全局坐标）
        self._dragging = False         # 是否已超过拖动阈值
        self._last_press_ts = 0.0      # 上次按下时间（自定义双击判定）
        self._last_press_pt = None
        self._hide_timer: QTimer | None = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowIcon(app_icon())   # 任务栏/Alt-Tab 使用项目图标
        self.setMinimumSize(360, 110)
        self.resize(cfg.get_int("ui", "window_width"), cfg.get_int("ui", "window_height"))
        # 窗口大小/位置改动后延迟写回 ini，避免拖动时反复写盘
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.timeout.connect(self._save_geometry)
        self._restore_geometry()
        self._build_toolbar()
        self.text_view = QTextBrowser(self)
        self._press_pos = None
        self.text_view.viewport().installEventFilter(self)
        self.text_view.setReadOnly(True)
        # 监听滚动条变化：用户向上滚动时锁定阅读位置
        self.text_view.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        # 监听锚点点击：点击"回到底部"链接时解除锁定
        self.text_view.anchorClicked.connect(self._on_anchor_clicked)
        self.text_view.setFrameShape(QFrame.Shape.NoFrame)
        self.text_view.setStyleSheet(
            "QTextBrowser{background:transparent;border:none;}"
            "QScrollBar:vertical{width:5px;background:transparent;margin:0;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,0.16);"
            "border-radius:2px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical"
            "{height:0;background:transparent;}")
        self.text_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 不开启文本选中：选中会和拖动窗口/拖滚动条抢鼠标（按下即开始选词）。
        # 只保留"链接可点"，让「↓ 有新字幕」链接还能用。
        self.text_view.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.text_view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self.text_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.toolbar.raise_()
        self._layout_text()
        self._refresh_text()   # 先显示空状态引导
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)
        self._build_tray()
        # 空闲自动收起：不翻译的时候别一直占着屏幕
        self._last_active = time.monotonic()
        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.timeout.connect(self._maybe_auto_hide)
        self._auto_hide_timer.start(20000)
        # 字幕模式：定时采样窗口背后的画面，亮底自动切黑字
        self._caption_timer = QTimer(self)
        self._caption_timer.timeout.connect(self._refresh_caption_color)

    # ---- 系统托盘 ----
    def _build_tray(self) -> None:
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            icon = app_icon()
            if icon.isNull():  # 图标文件缺失时退回内置绘制图标
                pm = QPixmap(64, 64)
                pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(26, 27, 28, 235))
                p.drawRoundedRect(2, 2, 60, 60, 14, 14)
                p.setBrush(QColor(82, 196, 26))
                p.drawEllipse(12, 12, 13, 13)  # 绿点
                p.setPen(QColor(255, 255, 255))
                f = QFont("Microsoft YaHei", 20, QFont.Weight.Bold)
                p.setFont(f)
                p.drawText(QRectF(4, 15, 56, 36), Qt.AlignmentFlag.AlignCenter, "译")
                p.end()
                icon = QIcon(pm)
            self.tray = QSystemTrayIcon(self)
            self.tray.setIcon(icon)
            self.tray.setToolTip("俄语网课实时翻译助手")
            menu = theme.GlassMenu()
            self.act_tray_toggle = menu.addAction("显示 / 隐藏窗口")
            menu.addSeparator()
            self.act_tray_start = menu.addAction("开始翻译")
            self.act_tray_pause = menu.addAction("暂停 / 继续")
            self.act_tray_end = menu.addAction("结束")
            self.act_tray_summary = menu.addAction("总结纪要")
            self.act_tray_record = menu.addAction("录音模式")
            menu.addSeparator()
            self.act_tray_open = menu.addAction("打开会议纪要目录")
            self.act_tray_config = menu.addAction("打开配置目录（API Key 在这里）")
            self.act_tray_settings = menu.addAction("设置")
            menu.addSeparator()
            self.act_tray_quit = menu.addAction("退出")
            self.act_tray_toggle.triggered.connect(self._toggle_visible)
            self.act_tray_start.triggered.connect(self._on_start)
            self.act_tray_pause.triggered.connect(self._on_pause)
            self.act_tray_end.triggered.connect(self._on_end)
            self.act_tray_summary.triggered.connect(self._on_summary)
            self.act_tray_record.triggered.connect(self._on_record)
            self.act_tray_open.triggered.connect(self._on_open)
            self.act_tray_config.triggered.connect(self._on_open_config)
            self.act_tray_settings.triggered.connect(self._on_settings)
            self.act_tray_quit.triggered.connect(self._on_quit)
            self._tray_menu = menu  # 防 GC
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()
        except Exception:  # noqa: BLE001 - 托盘不可用不影响主功能
            pass

    def _toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._toggle_visible()

    # ---- 工具条（悬停浮现） ----
    def _build_toolbar(self) -> None:
        """玻璃胶囊工具条：常用动作 + 「⋯」放低频动作，避免按钮挤成一排。"""
        self.toolbar = _GlassBar(self)
        self.toolbar.setGeometry(_BAR_MARGIN, _BAR_MARGIN,
                                 self.width() - 2 * _BAR_MARGIN, _TOOLBAR_H)
        lay = QHBoxLayout(self.toolbar)
        lay.setContentsMargins(7, 4, 7, 4)
        lay.setSpacing(2)
        self.toolbar.setStyleSheet(
            "QPushButton{background:transparent;border:none;color:rgba(236,241,251,0.86);"
            "border-radius:9px;padding:5px 10px;font-size:12px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.12);color:#FFFFFF;}"
            "QPushButton:pressed{background:rgba(255,255,255,0.06);}"
            "QPushButton:disabled{color:rgba(236,241,251,0.30);background:transparent;}"
            "QPushButton#primary{background:#8AB4FF;color:#08111F;"
            "font-weight:600;padding:5px 12px;}"
            "QPushButton#primary:hover{background:#A8C7FF;}"
            "QPushButton#primary:disabled{background:rgba(138,180,255,0.16);"
            "color:rgba(168,199,255,0.8);}"
            "QPushButton#more{font-size:15px;padding:0 2px;color:rgba(236,241,251,0.66);}"
            "QPushButton#more:hover{color:#FFFFFF;background:rgba(255,255,255,0.12);}"
            "QPushButton#barIcon{font-size:16px;padding:0 0 2px 0;"
            "color:rgba(236,241,251,0.66);}"
            "QPushButton#barIcon:hover{color:#FFFFFF;background:rgba(255,255,255,0.12);}"
        )

        self.btn_start = QPushButton("开始翻译")
        self.btn_start.setObjectName("primary")
        self.btn_start.setToolTip("开始识别并翻译（F5）")
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setToolTip("暂停 / 继续翻译（F6）")
        self.btn_end = QPushButton("结束")
        self.btn_end.setToolTip("结束本次课堂并保存转写（F7）")
        self.btn_summary = QPushButton("纪要")
        self.btn_summary.setToolTip("生成课堂纪要：对本次转写作总结（结束后可用）")
        self.btn_record = QPushButton("录音")
        self.btn_record.setToolTip("录音模式：缩成小图标，后台继续翻译 + 录音（F8）")
        self.btn_caption = QPushButton("字幕")
        self.btn_caption.setToolTip("全屏字幕模式：只留双语字幕，双击或右键退出（F9）")

        for b in (self.btn_start, self.btn_pause, self.btn_end,
                  self.btn_summary, self.btn_record, self.btn_caption):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(b)
        lay.addStretch(1)
        lay.addWidget(_sep())
        self.btn_hide = QPushButton()
        self.btn_hide.setObjectName("barIcon")
        self.btn_hide.setIcon(_minus_icon())
        self.btn_hide.setIconSize(QSize(18, 18))
        self.btn_hide.setFixedSize(28, 28)
        self.btn_hide.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_hide.setToolTip("收起窗口：不翻译时更省地方，翻译不受影响；点托盘图标恢复")
        self.btn_hide.clicked.connect(lambda: self._hide_to_tray(auto=False))
        lay.addWidget(self.btn_hide)
        self.btn_more = QPushButton("⋯")
        self.btn_more.setObjectName("more")
        self.btn_more.setFixedSize(30, 28)
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.setToolTip("更多：打开目录 / 设置 / 退出")
        lay.addWidget(self.btn_more)

        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_end.clicked.connect(self._on_end)
        self.btn_summary.clicked.connect(self._on_summary)
        self.btn_record.clicked.connect(self._on_record)
        self.btn_caption.clicked.connect(self._on_caption)
        self.btn_more.clicked.connect(self._show_more_menu)

        # 快捷键：窗口有焦点时生效（不注册全局热键，不和别的软件抢）
        for seq, slot in (
            ("F5", self._on_start), ("F6", self._on_pause), ("F7", self._on_end),
            ("F8", self._on_record), ("F9", self._on_caption),
            ("Ctrl+,", self._on_settings),
        ):
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.triggered.connect(slot)
            self.addAction(act)
        self.toolbar.hide()

    def _show_more_menu(self) -> None:
        menu = theme.GlassMenu(self)
        act_open = menu.addAction("打开纪要目录")
        act_cfg = menu.addAction("打开配置目录（API Key 在这里）")
        menu.addSeparator()
        act_settings = menu.addAction("设置…")
        menu.addSeparator()
        act_quit = menu.addAction("退出程序")
        act_open.triggered.connect(self._on_open)
        act_cfg.triggered.connect(self._on_open_config)
        act_settings.triggered.connect(self._on_settings)
        act_quit.triggered.connect(self._on_quit)
        menu.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))

    # ---- 收起 / 空闲自动收起 ----
    def _touch(self) -> None:
        """记录最近一次"有人在用"，空闲计时从这里算。"""
        self._last_active = time.monotonic()

    def _hide_to_tray(self, auto: bool = False) -> None:
        """收起到托盘：翻译继续在后台跑，窗口不再占屏幕。"""
        if self._mini_mode:
            self._exit_mini()
        self.toolbar.hide()
        self.hide()
        if auto:
            minutes = self._cfg.get_int("ui", "auto_hide_minutes")
            self.notify("窗口已自动收起",
                        f"已经 {minutes} 分钟没有翻译；点托盘图标可恢复，开始翻译时也会自动弹出")
        else:
            self.notify("窗口已收起", "翻译在后台继续；点任务栏托盘图标可恢复")

    def _maybe_auto_hide(self) -> None:
        """不翻译、也没人动过窗口，静置超过设定分钟数就收起来。"""
        try:
            minutes = self._cfg.get_int("ui", "auto_hide_minutes")
        except Exception:  # noqa: BLE001
            return
        if minutes <= 0 or not self.isVisible() or self._mini_mode or self._caption_mode:
            return
        state = getattr(getattr(self._ctl, "state", None), "value", "idle")
        if state in ("starting", "running", "paused", "ending", "summarizing"):
            self._touch()
            return
        if self.underMouse() or self.toolbar.underMouse():
            self._touch()
            return
        if time.monotonic() - self._last_active >= minutes * 60:
            self._hide_to_tray(auto=True)

    def set_controller(self, ctl) -> None:
        self._ctl = ctl
        self.update_controls()

    def _on_start(self) -> None:
        self._touch()
        if not self.isVisible():      # 从托盘菜单开始翻译：把窗口叫回来
            self.show()
            self.raise_()
        if self._ctl:
            self._ctl.start()

    def _on_pause(self) -> None:
        if self._ctl:
            self._ctl.toggle_pause()

    def _on_end(self) -> None:
        if self._ctl:
            self._ctl.end()

    def _on_summary(self) -> None:
        """手动生成课堂纪要（默认不再自动生成）。"""
        if self._ctl:
            self._ctl.summarize()

    def _enter_mini(self) -> None:
        """录音模式：最小化为「录」字小图标，后台继续翻译 + 录音 + 纪要。"""
        self._normal_geom = self.geometry()
        self._mini_mode = True
        self.setMinimumSize(0, 0)
        self.toolbar.hide()
        self.text_view.hide()
        self.resize(44, 44)
        self.update()

    def _exit_mini(self) -> None:
        """双击「录」图标：恢复实时翻译显示框。"""
        self._mini_mode = False
        self.setMinimumSize(360, 110)
        if self._normal_geom is not None:
            self.setGeometry(self._normal_geom)
        self.text_view.show()
        self._layout_text()
        self.update()

    def _on_record(self) -> None:
        # 录音模式依赖翻译运行：未开始翻译时不执行
        st = getattr(getattr(self._ctl, "state", None), "value", "idle")
        if st not in ("running", "paused"):
            return
        if self._mini_mode:
            self._exit_mini()
        else:
            self._enter_mini()

    def _on_open(self) -> None:
        if self._ctl:
            self._ctl.open_sessions()

    def _on_open_config(self) -> None:
        if self._ctl:
            self._ctl.open_config_dir()


    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """字幕模式：按住拖动移动窗口、双击或右键退出；列表模式透传（不选文字）。

        手感优化点：
          1. 拖动有 4px 阈值 —— 手抖不会让窗口乱跳，"双击"也就不会因为窗口位移而落空；
          2. 自研双击判定（450ms / 14px），比 Qt 默认更宽松，双击更容易触发；
          3. 右键也可退出，作为双击之外的第二条退路；
          4. 进入字幕模式自动关闭文本选择并换成"移动"光标，避免和选文字抢事件。
        """
        if obj is not self.text_view.viewport():
            return False
        if self._mini_mode or not self._caption_mode:
            return False
        t = event.type()
        if t == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.RightButton:
                self._on_caption_click()   # 右键退出字幕模式
                return True
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            gp = event.globalPosition().toPoint()
            now = time.monotonic()
            if (self._last_press_pt is not None
                    and (now - self._last_press_ts) * 1000 <= _CAPTION_DBLCLICK_MS
                    and (gp - self._last_press_pt).manhattanLength() <= _CAPTION_DBLCLICK_PX):
                self._last_press_pt = None
                self._press_global = None
                self._dragging = False
                self._on_caption_click()   # 自定义双击：退出字幕模式
                return True
            self._last_press_ts = now
            self._last_press_pt = gp
            self._press_global = gp
            self._press_geom = self.geometry()
            self._dragging = False         # 先不移动，等超过阈值
            return True
        if t == QEvent.Type.MouseButtonDblClick:
            self._on_caption_click()       # Qt 原生双击（兜底；已经在上面处理过则这里是空操作）
            return True
        if t == QEvent.Type.MouseMove:
            if self._press_global is None or not (event.buttons() & Qt.MouseButton.LeftButton):
                return False
            gp = event.globalPosition().toPoint()
            if not self._dragging:
                if (gp - self._press_global).manhattanLength() < _CAPTION_DRAG_PX:
                    return True            # 未达阈值：不移动，保证双击判定成立
                self._dragging = True
            self.move(self._press_geom.x() + gp.x() - self._press_global.x(),
                      self._press_geom.y() + gp.y() - self._press_global.y())
            return True
        if t == QEvent.Type.MouseButtonRelease:
            if self._dragging:
                # 刚拖动过：不要把这次按下当成"双击的前半次"，
                # 否则用户拖完再快速点一下会被误判成双击而退出字幕模式
                self._last_press_pt = None
            self._press_global = None
            self._dragging = False
            return True
        return False

    def _on_caption(self) -> None:
        """进入/退出字幕模式。"""
        # 字幕模式依赖翻译运行：未开始翻译时不执行
        st = getattr(getattr(self._ctl, "state", None), "value", "idle")
        if st not in ("running", "paused"):
            return
        self._set_caption_mode(not self._caption_mode)

    def _set_caption_mode(self, on: bool) -> None:
        """切换字幕模式：隐藏工具栏、关掉文本选择（让拖动更跟手）、换光标并给操作提示。"""
        if self._caption_mode == on:
            return
        self._caption_mode = on
        self.btn_caption.setText("退出字幕" if on else "字幕")
        if on:
            self.toolbar.hide()
            # 字幕模式：文字不可选中（按下就变成选词会让拖动/双击失灵）
            self.text_view.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            self.text_view.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
            self._refresh_caption_color()
            self._caption_timer.start(1500)
            self.notify("字幕模式", "按住可拖动位置；双击或点右键退出字幕模式")
        else:
            self._caption_timer.stop()
            # 回到列表模式：同样不允许选中，只保留链接可点
            self.text_view.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self.text_view.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            if self.underMouse():
                self.toolbar.show()
        self._press_global = None
        self._dragging = False
        self._last_press_pt = None
        self._layout_text()
        self._refresh_text()
        self.update()

    def _on_caption_click(self) -> None:
        """字幕模式下双击/右键：切回列表模式。"""
        self._set_caption_mode(False)

    # ---- 字幕模式：文字颜色跟随底色 ----
    def _sample_backdrop_luminance(self) -> float:
        """抓窗口所在区域，返回中位亮度；抓不到返回 -1。"""
        try:
            geo = self.frameGeometry()
            screen = (QGuiApplication.screenAt(geo.center())
                      or QGuiApplication.primaryScreen())
            if screen is None:
                return -1.0
            local = geo.translated(-screen.geometry().topLeft())
            pm = screen.grabWindow(0, local.x(), local.y(),
                                   max(1, local.width()), max(1, local.height()))
            if pm.isNull():
                return -1.0
            return _median_luminance(pm.toImage())
        except Exception:  # noqa: BLE001 - 抓屏失败不能影响字幕
            return -1.0

    def _refresh_caption_color(self) -> None:
        """auto：亮底切黑字、暗底白字；也可以固定成某个颜色。"""
        if not self._caption_mode or self._mini_mode:
            return
        setting = (self._cfg.get("ui", "caption_text_color") or "auto").strip()
        if setting.lower() not in ("auto", ""):
            qc = QColor(setting)
            if qc.isValid() and qc != self._caption_color:
                self._caption_color = qc
                self._refresh_text()
                self.update()
            return
        lum = self._sample_backdrop_luminance()
        if lum < 0:
            return  # 抓不到底图就沿用上一次的判断
        qc = _pick_caption_color(lum)
        if qc != self._caption_color:
            self._caption_color = qc
            self._refresh_text()
            self.update()

    def _on_settings(self) -> None:
        self._touch()
        SettingsDialog(self._cfg, self).exec()
        self._touch()   # 设置里待久了，回到窗口别立刻被自动收起

    def _on_quit(self) -> None:
        if self._ctl:
            self._ctl.quit()

    def update_controls(self) -> None:
        st = getattr(getattr(self._ctl, "state", None), "value", "idle")
        paused = bool(getattr(self._ctl, "paused", False))
        running = st in ("running", "paused")
        ending = st == "ending"
        summarizing = st == "summarizing"
        self.btn_start.setText("翻译中…" if (running or ending) else "开始翻译")
        self.btn_start.setEnabled(st in ("idle", "error"))
        # 纪要按钮：结束后、且本次课堂有转写内容时才可用（默认不再自动生成纪要）
        self.btn_summary.setText("纪要中…" if summarizing else "纪要")
        can_sum = bool(self._ctl and getattr(self._ctl, "can_summarize", lambda: False)())
        self.btn_summary.setEnabled(can_sum)
        self.btn_pause.setText("继续" if paused else "暂停")
        self.btn_pause.setEnabled(st in ("running", "paused"))
        self.btn_end.setText("纪要生成中…" if ending else "结束")
        self.btn_end.setEnabled(running)
        # 录音和字幕功能依赖翻译运行：未开始翻译时禁用，开始后才可用
        self.btn_record.setEnabled(running)
        self.btn_caption.setEnabled(running)
        # 结束翻译时自动退出字幕模式和录音模式
        if not running and not ending:
            if self._caption_mode:
                self._caption_mode = False
                self.btn_caption.setText("字幕")
                self._layout_text()
                self._refresh_text()
            if self._mini_mode:
                self._exit_mini()

    # ---- 鼠标：悬停显示工具条 ----
    def enterEvent(self, event) -> None:  # noqa: N802
        if self._mini_mode:
            return
        # 字幕模式下不显示工具栏，只显示纯字幕
        if self._caption_mode:
            return
        self._touch()
        self.toolbar.show()
        self._layout_text()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_edge = None
        if self._mini_mode:
            return
        if self._hide_timer is None:
            self._hide_timer = QTimer(self)
            self._hide_timer.setSingleShot(True)
            self._hide_timer.timeout.connect(self._hide_toolbar)
        self._hide_timer.start(300)
        super().leaveEvent(event)

    def _hide_toolbar(self) -> None:
        if not self.underMouse() and not self.toolbar.underMouse():
            self.toolbar.hide()
            self.update()

    # ---- 鼠标：拖动与四边四角缩放 ----
    def _edge_at(self, pos) -> str | None:
        """只检测右下角缩放区域（其他边缘用于拖动）。"""
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        # 右下角 24x24 区域用于缩放
        if x >= w - 24 and y >= h - 24:
            return "br"
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._touch()
        # 字幕模式下右键退出字幕模式
        if self._caption_mode and not self._mini_mode \
                and event.button() == Qt.MouseButton.RightButton:
            self._on_caption_click()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            edge = None if self._mini_mode else self._edge_at(event.position().toPoint())
            self._mode = edge or "move"
            self._drag_pos = event.globalPosition().toPoint()
            self._press_geom = self.geometry()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.globalPosition().toPoint()
        if self._drag_pos is None:
            if self._mini_mode:
                self.setCursor(Qt.CursorShape.ArrowCursor)
                return
            edge = self._edge_at(event.position().toPoint())
            if edge != self._hover_edge:
                self._hover_edge = edge
                self.update()
            if edge is None and event.position().y() > self.height() - _FOOTER_H:
                self.setCursor(Qt.CursorShape.OpenHandCursor)  # 底部条 = 拖动区
                return
            cursor = {
                "left": Qt.CursorShape.SizeHorCursor,
                "right": Qt.CursorShape.SizeHorCursor,
                "top": Qt.CursorShape.SizeVerCursor,
                "bottom": Qt.CursorShape.SizeVerCursor,
                "tl": Qt.CursorShape.SizeFDiagCursor,
                "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor,
                "bl": Qt.CursorShape.SizeBDiagCursor,
            }.get(edge or "")
            self.setCursor(cursor or Qt.CursorShape.ArrowCursor)
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            self._drag_pos = None
            return
        dx = pos.x() - self._drag_pos.x()
        dy = pos.y() - self._drag_pos.y()
        g = self._press_geom
        if self._mode == "move":
            self.move(g.x() + dx, g.y() + dy)
            return
        # 缩放
        left = right = top = bottom = None
        if self._mode in ("left", "tl", "bl"):
            left = g.x() + dx
        if self._mode in ("right", "tr", "br"):
            right = g.right() + dx
        if self._mode in ("top", "tl", "tr"):
            top = g.y() + dy
        if self._mode in ("bottom", "bl", "br"):
            bottom = g.bottom() + dy
        left = left if left is not None else g.x()
        right = right if right is not None else g.right()
        top = top if top is not None else g.y()
        bottom = bottom if bottom is not None else g.bottom()
        if right - left < self.minimumWidth():
            if left != g.x():
                left = right - self.minimumWidth()
            else:
                right = left + self.minimumWidth()
        if bottom - top < self.minimumHeight():
            if top != g.y():
                top = bottom - self.minimumHeight()
            else:
                bottom = top + self.minimumHeight()
        self.setGeometry(left, top, right - left, bottom - top)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_pos = None
        self._mode = None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """双击：录音小窗 → 恢复；字幕模式 → 退出字幕模式。"""
        if self._mini_mode:
            self._exit_mini()
            return
        if self._caption_mode:
            self._on_caption_click()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.toolbar.setGeometry(_BAR_MARGIN, _BAR_MARGIN,
                                 max(0, self.width() - 2 * _BAR_MARGIN), _TOOLBAR_H)
        self._layout_text()
        self._schedule_geometry_save()
        super().resizeEvent(event)

    def moveEvent(self, event) -> None:  # noqa: N802
        self._schedule_geometry_save()
        super().moveEvent(event)

    # ---- 窗口几何记忆：下次打开还在上次的位置和大小 ----
    def _restore_geometry(self) -> None:
        x = self._cfg.get_int("ui", "window_x")
        y = self._cfg.get_int("ui", "window_y")
        if x < -1000 or y < -1000:
            return  # 没记录过
        rect = QRect(x, y, self.width(), self.height())
        try:
            from PyQt6.QtGui import QGuiApplication
            if any(s.availableGeometry().intersects(rect)
                   for s in QGuiApplication.screens()):
                self.setGeometry(rect)
        except Exception:  # noqa: BLE001
            pass

    def _schedule_geometry_save(self) -> None:
        tim = getattr(self, "_geom_timer", None)
        if tim is None or self._mini_mode or self._caption_mode:
            return
        tim.start(700)

    def _save_geometry(self) -> None:
        if self._mini_mode or self._caption_mode:
            return
        g = self.geometry()
        try:
            self._cfg.set("ui", "window_width", str(g.width()))
            self._cfg.set("ui", "window_height", str(g.height()))
            self._cfg.set("ui", "window_x", str(g.x()))
            self._cfg.set("ui", "window_y", str(g.y()))
        except Exception:  # noqa: BLE001 - 配置只读/磁盘满时不该影响使用
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._geom_timer.stop()
            self._save_geometry()   # 关窗口前把位置/大小落盘
        except Exception:  # noqa: BLE001
            pass
        if self._ctl:
            self._ctl.quit()
        event.accept()

    # ---- 文本视图（只读展示；不开启选中，避免和拖动/滚轮抢鼠标）----
    def _layout_text(self) -> None:
        if self._mini_mode:
            return
        e = _EDGE
        # 字幕模式下文本占满整个窗口（去掉边距和底部预留）
        if self._caption_mode:
            self.text_view.setGeometry(0, 0, self.width(), self.height())
            return
        top = (_TOOLBAR_H + _BAR_MARGIN + 8) if self.toolbar.isVisible() else e
        self.text_view.setGeometry(e, top, self.width() - 2 * e,
                                   self.height() - top - _FOOTER_H - 6)

    def _scroll_bottom(self, force: bool = False) -> None:
        """延迟到布局完成后滚动到底部（保证最新文本在下方可见）。"""
        # 用户向上滚动锁定阅读位置时，不自动滚到底部
        if self._user_scrolled and not force:
            return
        QTimer.singleShot(
            0,
            lambda: self.text_view.verticalScrollBar().setValue(
                self.text_view.verticalScrollBar().maximum()),
        )

    def _on_scroll_changed(self, value: int) -> None:
        """滚动条变化：用户向上滚动时锁定阅读位置，滚到底部时解除锁定。"""
        sb = self.text_view.verticalScrollBar()
        if value >= sb.maximum() - 2:
            # 滚到底部附近，解除锁定
            if self._user_scrolled:
                self._user_scrolled = False
                self._pending_count = 0
        else:
            # 向上滚动，锁定阅读位置
            if not self._user_scrolled:
                self._user_scrolled = True

    def _on_anchor_clicked(self, url) -> None:
        """点击"回到底部"链接时解除锁定并滚动到底部。"""
        if url.toString() == "scroll-bottom":
            self._user_scrolled = False
            self._pending_count = 0
            self._scroll_bottom(force=True)
            self._refresh_text()

    def _refresh_text(self) -> None:
        """按当前条目生成富文本（角色分色），最新内容滚到底部。"""
        esc = html.escape
        font_pt = self._cfg.get_int("ui", "font_size")
        # 空窗口引导：别让第一次打开只看到一块黑玻璃
        if not self._entries and self._preview_entry is None and not self._caption_mode:
            st = getattr(getattr(self._ctl, "state", None), "value", "idle")
            if st in ("starting", "running", "paused"):
                head, sub = "正在听…", "老师开口后，俄语原文和中文会在这里逐行出现"
            else:
                head, sub = "点「开始翻译」或按 F5 开始", "字幕会实时出现在这里"
            self.text_view.setHtml(
                f'<div style="text-align:center;color:rgba(226,233,246,0.64);'
                f'font-size:{max(9, font_pt)}pt;line-height:1.9;margin-top:14px;">'
                f'{head}<br/><span style="font-size:{max(8, font_pt - 1)}pt;'
                f'color:rgba(226,233,246,0.36);">{sub}</span></div>')
            return
        # 字幕模式：显示最近 N 段（1 段 = 一句原文 + 它的译文），越旧越淡
        if self._caption_mode:
            preview = self._preview_entry
            n = max(1, min(5, self._cfg.get_int("ui", "caption_segments")))
            segs = [x for x in self._entries if x.status == "translated"][-n:]
            if not segs:
                segs = [x for x in self._entries if x.status == "recognized"][-n:]
            if not segs:
                # 还没有可用字幕：先显示正在识别的临时文本，避免空窗
                if preview is not None and preview.ru_text:
                    p_label = "老师" if preview.speaker == "teacher" else "我"
                    self.text_view.setHtml(
                        f'<div style="font-size:{font_pt}pt;padding:6px 12px 0 18px;'
                        f'color:#9AA6BC;font-style:italic;text-align:left;'
                        f'line-height:1.5;">[{p_label}] {esc(preview.ru_text)}'
                        f' <i>（识别中…）</i></div>')
                else:
                    self.text_view.setHtml(
                        f'<div style="font-size:{font_pt}pt;'
                        f'color:{_fade_color(self._caption_color.name(), 0.7, _CAPTION_FADE_BG)};'
                        f'padding:6px 12px 0 18px;text-align:left;">听译中…</div>')
                self._scroll_bottom()
                return

            cap_hex = self._caption_color.name()
            blocks: list[str] = []
            total = len(segs)
            for i, seg in enumerate(segs):
                # 越旧越淡：最新 100%，上一段 82%，更早 62%
                fade = 1.0 if i == total - 1 else (0.82 if i == total - 2 else 0.62)
                label = "老师" if seg.speaker == "teacher" else "我"
                color = _fade_color(cap_hex, fade, _CAPTION_FADE_BG)
                ru_color = color
                zh_color = color
                ru_disp = seg.corrected_ru or seg.ru_text
                mark = (f' <span style="color:{_fade_color(cap_hex, fade * 0.8, _CAPTION_FADE_BG)};'
                        f'font-size:{max(7, font_pt - 2)}pt;">(已纠错)</span>'
                        if seg.corrected_ru else "")
                blocks.append(
                    f'<div style="margin-bottom:10px;">'
                    f'<div style="font-size:{font_pt}pt;color:{ru_color};'
                    f'line-height:1.35;text-align:left;">[{label}] {esc(ru_disp)}{mark}</div>'
                    f'<div style="font-size:{min(20, font_pt + 3)}pt;color:{zh_color};'
                    f'line-height:1.35;text-align:left;margin-top:2px;">'
                    f'{esc(seg.zh_text or "翻译中…")}</div></div>')
            # 下一句正在识别：小字灰色，放在最下面
            if preview is not None and preview.ru_text:
                p_label = "老师" if preview.speaker == "teacher" else "我"
                blocks.append(
                    f'<div style="font-size:{max(9, font_pt - 2)}pt;'
                    f'color:{_fade_color(cap_hex, 0.55, _CAPTION_FADE_BG)};'
                    f'font-style:italic;text-align:left;margin-top:2px;">'
                    f'[{p_label}] {esc(preview.ru_text)} <i>（识别中…）</i></div>')
            self.text_view.setHtml(
                f'<div style="padding:4px 12px 4px 18px;">{"".join(blocks)}</div>')
            self._scroll_bottom()
            return
        limit = self._cfg.get_int("ui", "history_lines")
        font_pt = self._cfg.get_int("ui", "font_size")
        tc = self._cfg.get("ui", "teacher_color")
        mc = self._cfg.get("ui", "me_color")
        zc = self._cfg.get("ui", "text_color")
        esc = html.escape
        parts = []
        for seg in self._entries[-limit:]:
            label = "老师" if seg.speaker == "teacher" else "我"
            color = mc if seg.speaker == "me" else tc
            ru_disp = seg.corrected_ru or seg.ru_text
            if seg.status == "failed":
                parts.append(
                    f'<span style="color:{color};">[{label}]（翻译失败，保留原文）'
                    f'{esc(ru_disp)}</span>')
            elif seg.status == "recognized":
                parts.append(
                    f'<span style="color:#9EACEA;">[{label}] {esc(ru_disp)}'
                    f' <i>（翻译中…）</i></span>')
            else:
                mark = (f' <span style="color:#8a8a8a;font-size:{max(5, font_pt - 3)}pt;">'
                        f'(已纠错)</span>' if seg.corrected_ru else "")
                parts.append(
                    f'<span style="color:{color};">[{label}] {esc(ru_disp)}{mark}</span>')
                if seg.zh_text:
                    parts.append(f'<span style="color:{zc};">{esc(seg.zh_text)}</span>')
        # 临时字幕（中间识别结果）：灰色显示在最后
        if self._preview_entry is not None:
            p = self._preview_entry
            label = "老师" if p.speaker == "teacher" else "我"
            parts.append(
                f'<span style="color:#888888;font-style:italic;">[{label}] {html.escape(p.ru_text)}'
                f' <i>（识别中…）</i></span>')
        # 阅读锁定提示：用户向上滚动后显示"有 N 条新字幕"
        if self._user_scrolled and self._pending_count > 0:
            parts.append(
                f'<div style="text-align:center;margin-top:4px;">'
                f'<a href="scroll-bottom" style="color:#52C41A;text-decoration:none;'
                f'font-size:{max(9, font_pt-1)}pt;">↓ 有 {self._pending_count} 条新字幕，点击回到底部</a>'
                f'</div>')
        body = "<br/>".join(parts)
        self.text_view.setHtml(
            f'<div style="font-size:{font_pt}pt;line-height:1.35;">{body}</div>')
        self._scroll_bottom()

    # ---- 数据 ----
    def push(self, seg: TranscriptSegment) -> None:
        self._touch()
        # 临时字幕（中间识别结果）：单独保存，不进入历史列表
        if seg.status == "preview":
            self._preview_entry = seg
            self._refresh_text()
            self.update()
            return
        # 收到最终结果时，清除临时字幕
        if seg.status in ("recognized", "translated", "failed"):
            self._preview_entry = None
        # 同一条字幕会经历 recognized → translated/failed，按 seq 原位覆盖。
        for i, old in enumerate(self._entries):
            if old.seq == seg.seq:
                self._entries[i] = seg
                break
        else:
            self._entries.append(seg)
            # 用户锁定阅读位置时，累计未读消息数
            if self._user_scrolled:
                self._pending_count += 1
        limit = self._cfg.get_int("ui", "history_lines")
        if len(self._entries) > limit:
            self._entries = self._entries[-limit:]
        self._refresh_text()
        self.update()

    def set_status(self, message: str) -> None:
        """只在 Qt 主线程调用（后台线程请用 status_changed 信号）。"""
        changed = message != self._status
        self._status = message
        self._touch()
        # 还没有字幕时，状态变化要顺带刷新空状态引导（开始翻译 → 正在听…）
        if changed and not self._entries and self._preview_entry is None:
            self._refresh_text()
        self.update()

    def notify(self, title: str, message: str) -> None:
        """系统托盘气泡通知（例如纪要生成完成）。"""
        try:
            tray = getattr(self, "tray", None)
            if tray is not None and tray.isVisible():
                tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)
        except Exception:  # noqa: BLE001
            pass

    def set_channels(self, text: str) -> None:
        """通道健康状态行（老师/我 有声音·静音），只在 Qt 主线程调用。"""
        if text == self._channels_text:
            return
        self._channels_text = text
        self.update()

    def _poll(self) -> None:
        q: "queue.Queue" = getattr(self, "disp_q", None)
        if q is None:
            return
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            try:
                if isinstance(item, TranscriptSegment):
                    self.push(item)
                else:
                    self.set_status(str(item))
            except Exception:  # noqa: BLE001 - 渲染异常不能让 Qt 槽炸掉整个进程
                import traceback
                traceback.print_exc()

    # ---- 绘制 ----
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        opacity = max(0, min(100, self._cfg.get_int("ui", "opacity"))) / 100.0
        alpha = 0.92 * opacity

        if self._caption_mode and not self._mini_mode:
            painter.end()
            return  # 字幕模式：完全无背景，只留文字（双击文字回到窗口）

        if self._mini_mode:
            theme.paint_glass(painter, QRectF(self.rect()), 14, alpha=max(0.7, alpha))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.DANGER))
            painter.drawEllipse(QPointF(13, self.height() / 2), 3.5, 3.5)
            painter.setPen(QColor(255, 255, 255, 235))
            painter.setFont(theme.font(11, QFont.Weight.DemiBold))
            painter.drawText(QRectF(19, 0, self.width() - 20, self.height()),
                             Qt.AlignmentFlag.AlignCenter, "录")
            painter.end()
            return

        theme.paint_glass(painter, QRectF(self.rect()), alpha=alpha,
                          hover=bool(self._hover_edge))

        status_font = theme.font(8.5)
        painter.setFont(status_font)
        cy = self.height() - _FOOTER_H / 2
        # 左下的"抓这里拖动"握把
        painter.setPen(QPen(QColor(233, 238, 249, 80), 1.3))
        for i in range(3):
            painter.drawLine(9, int(cy - 3 + i * 3), 15, int(cy - 3 + i * 3))
        # 状态：小圆点 + 文案（出错时变琥珀色，别让人错过 Key 没配）
        ok = "有声音" in self._channels_text
        bad = any(k in self._status for k in ("失败", "错误", "异常", "未配置", "超时"))
        painter.setPen(Qt.PenStyle.NoPen)
        if bad:
            dot = QColor(theme.WARN)
        elif ok:
            dot = QColor(theme.OK_GREEN)
        else:
            dot = QColor(200, 210, 230, 110)
        painter.setBrush(dot)
        painter.drawEllipse(QPointF(26, cy), 3, 3)
        painter.setPen(QColor(theme.WARN) if bad else QColor(226, 233, 246, 168))
        painter.drawText(QRectF(34, self.height() - _FOOTER_H, 260, _FOOTER_H),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self._status)
        # 通道健康状态（老师 / 我）
        if self._channels_text:
            painter.setPen(QColor(91, 227, 166, 235) if ok else QColor(200, 210, 230, 140))
            painter.drawText(QRectF(0, self.height() - _FOOTER_H, self.width() - 30, _FOOTER_H),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             self._channels_text)
        # 右下角缩放抓手（三线）
        gx, gy = self.width() - 18, self.height() - 18
        painter.setPen(QPen(QColor(255, 255, 255, 100), 1.8))
        for i in range(3):
            painter.drawLine(gx + 2 + i * 4, gy + 14, gx + 14, gy + 2 + i * 4)
        painter.end()


class ColorButton(QPushButton):
    """色块按钮：点击弹出系统取色器；改色后发 changed 供预览刷新。"""

    changed = pyqtSignal(str)

    def __init__(self, color_hex: str):
        super().__init__()
        self._hex = color_hex
        self.setFixedSize(76, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh()
        self.clicked.connect(self._pick)

    def _refresh(self) -> None:
        c = QColor(self._hex)
        fg = "#000000" if c.lightness() > 150 else "#FFFFFF"
        self.setText(self._hex)
        self.setStyleSheet(
            f"QPushButton{{background:{self._hex};color:{fg};border:1px solid "
            f"rgba(255,255,255,0.22);border-radius:7px;font-size:11px;padding:0;}}"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._hex), self, "选择颜色")
        if c.isValid():
            self._hex = c.name()
            self._refresh()
            self.changed.emit(self._hex)

    def hex(self) -> str:
        return self._hex


class GlassPreview(QWidget):
    """设置里的实时预览：用同一套玻璃画法，改字号/透明度/颜色立刻能看到。"""

    def __init__(self, cfg):
        super().__init__()
        self._font_pt = cfg.get_int("ui", "font_size")
        self._alpha = cfg.get_int("ui", "opacity") / 100.0
        self._teacher = QColor(cfg.get("ui", "teacher_color"))
        self._zh = QColor(cfg.get("ui", "text_color"))
        self._skin = cfg.get("ui", "skin") or "dark"
        self.setFixedHeight(96)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

    def set_skin(self, key: str) -> None:
        self._skin = key if key in theme.SKINS else "dark"
        self.update()

    def set_values(self, font_pt: int, opacity: int,
                   teacher: str, zh: str) -> None:
        self._font_pt = font_pt
        self._alpha = opacity / 100.0
        self._teacher = QColor(teacher)
        self._zh = QColor(zh)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        # 先画一小块"课堂画面"，再盖玻璃：这样能直观看出透出多少内容
        bg = QLinearGradient(r.topLeft(), r.bottomRight())
        bg.setColorAt(0.0, QColor("#EEF2FB"))
        bg.setColorAt(1.0, QColor("#D3DEF2"))
        p.fillRect(r, bg)
        for cx, cy, rad, col in ((0.22, 0.3, 0.5, QColor(120, 165, 255, 170)),
                                 (0.82, 0.7, 0.55, QColor(255, 150, 200, 150))):
            g = QRadialGradient(QPointF(cx * r.width(), cy * r.height()),
                                rad * r.width())
            g.setColorAt(0.0, col)
            c = QColor(col)
            c.setAlpha(0)
            g.setColorAt(1.0, c)
            p.setBrush(g)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx * r.width(), cy * r.height()),
                          rad * r.width(), rad * r.width())
        theme.paint_glass(p, r, alpha=0.92 * self._alpha,
                          skin=theme.SKINS.get(self._skin, theme.DARK_SKIN))
        p.setFont(theme.font(max(6.0, self._font_pt - 1)))
        p.setPen(self._teacher)
        p.drawText(QRectF(14, 12, self.width() - 28, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "[老师] Производная — скорость изменения функции.")
        p.setFont(theme.font(max(7.0, self._font_pt + 2), QFont.Weight.DemiBold))
        p.setPen(self._zh)
        p.drawText(QRectF(14, 38, self.width() - 28, 26),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "[老师] 导数就是函数的变化率。")
        p.setFont(theme.font(8))
        p.setPen(QColor(226, 233, 246, 150))
        p.drawText(QRectF(14, self.height() - 24, self.width() - 28, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "预览：字号 / 不透明度 / 文字颜色 的实际效果")
        p.end()


def _scroll_page(page: QWidget) -> QScrollArea:
    """把设置页塞进滚动区：AI 模型页很长，小屏幕也不会顶出屏幕外。

    外面再包一层 + 底部 stretch，避免 QFormLayout 被拉高、把每行撑开。
    """
    lay = page.layout()
    if isinstance(lay, QFormLayout):
        # 中文标签比英文宽，给左列一个下限，避免被右侧输入框挤成"幕字号"
        for row in range(lay.rowCount()):
            item = lay.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if item is not None and item.widget() is not None:
                item.widget().setMinimumWidth(104)
    inner = QWidget()
    v = QVBoxLayout(inner)
    v.setContentsMargins(2, 2, 8, 2)
    v.setSpacing(0)
    v.addWidget(page)
    v.addStretch(1)
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setWidget(inner)
    sa.viewport().setAutoFillBackground(False)
    inner.setAutoFillBackground(False)
    page.setAutoFillBackground(False)
    return sa


class SettingsDialog(theme.GlassDialog):
    """设置（显示 / 设备 / 输出 / 语言 / AI 模型 五页，确定后写回 ini）。"""

    test_done = pyqtSignal(str, bool)  # 服务连通性测试结果（后台线程 → 主线程）

    def __init__(self, cfg, parent=None):
        super().__init__("设置", parent)
        self._cfg = cfg
        self.set_subtitle("HiLanguageOL · 课堂实时翻译")
        self.test_done.connect(self._on_test_done)
        self.setMinimumSize(600, 540)
        tabs = QTabWidget()

        # ---- 显示 ----
        show = QWidget()
        f1 = QFormLayout(show)
        f1.setContentsMargins(0, 4, 0, 4)
        f1.setVerticalSpacing(10)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # 实时预览：省得改完参数还得关掉窗口才能看到效果
        self.preview = GlassPreview(cfg)
        f1.addRow(self.preview)

        self.skin_cb = QComboBox()
        for key, sk in theme.SKINS.items():
            self.skin_cb.addItem(sk.label, key)
        idx_skin = self.skin_cb.findData(cfg.get("ui", "skin") or "dark")
        self.skin_cb.setCurrentIndex(idx_skin if idx_skin >= 0 else 0)
        f1.addRow("玻璃皮肤", self.skin_cb)
        self.skin_cb.currentIndexChanged.connect(self._on_skin_changed)

        self.font_size = QSpinBox()
        self.font_size.setRange(_FONT_MIN, _FONT_MAX)
        self.font_size.setValue(cfg.get_int("ui", "font_size"))
        self.font_size.setSuffix(" pt")
        f1.addRow("字幕字号", self.font_size)

        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(20, 100)
        self.opacity.setValue(max(20, cfg.get_int("ui", "opacity")))
        self.opacity.setPageStep(5)
        op_row = QHBoxLayout()
        op_row.setSpacing(10)
        self.lbl_opacity = QLabel(f"{self.opacity.value()}%")
        self.lbl_opacity.setObjectName("value")
        self.lbl_opacity.setFixedWidth(40)
        self.lbl_opacity.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        op_row.addWidget(self.opacity, 1)
        op_row.addWidget(self.lbl_opacity)
        f1.addRow("面板不透明度", op_row)
        self.history = QSpinBox()
        self.history.setRange(3, 20)
        self.history.setValue(cfg.get_int("ui", "history_lines"))
        f1.addRow("历史条数", self.history)

        self.caption_segments = QSpinBox()
        self.caption_segments.setRange(1, 5)
        self.caption_segments.setValue(cfg.get_int("ui", "caption_segments"))
        self.caption_segments.setSuffix(" 段")
        self.caption_segments.setToolTip("1 段 = 一句识别原文 + 它的译文")
        f1.addRow("字幕模式显示", self.caption_segments)

        self.caption_color_cb = QComboBox()
        for label, val in (("自动（跟随底色）", "auto"), ("白色", "#FFFFFF"), ("黑色", "#000000")):
            self.caption_color_cb.addItem(label, val)
        idx_cc = self.caption_color_cb.findData(
            (cfg.get("ui", "caption_text_color") or "auto").strip())
        self.caption_color_cb.setCurrentIndex(idx_cc if idx_cc >= 0 else 0)
        self.caption_color_cb.setToolTip("字幕模式没有背景：亮底会自动切黑字，暗底切白字；也可固定")
        f1.addRow("字幕模式文字", self.caption_color_cb)

        self.auto_hide = QComboBox()
        for mins, label in ((0, "从不"), (1, "1 分钟"), (3, "3 分钟"),
                            (5, "5 分钟"), (10, "10 分钟"), (30, "30 分钟")):
            self.auto_hide.addItem(label, mins)
        idx_hide = self.auto_hide.findData(cfg.get_int("ui", "auto_hide_minutes"))
        self.auto_hide.setCurrentIndex(idx_hide if idx_hide >= 0 else 0)
        self.auto_hide.setToolTip("不翻译时静置这么久就收进托盘；点托盘图标或开始翻译会重新出现")
        f1.addRow("不翻译时自动收起", self.auto_hide)
        # 三种文字颜色放一行，省两行高度（小屏也不用滚到底才能配色）
        self.teacher_color = ColorButton(cfg.get("ui", "teacher_color"))
        self.me_color = ColorButton(cfg.get("ui", "me_color"))
        self.zh_color = ColorButton(cfg.get("ui", "text_color"))
        color_row = QHBoxLayout()
        color_row.setSpacing(6)
        for tag, cb in (("老师", self.teacher_color), ("我", self.me_color),
                        ("中文", self.zh_color)):
            lbl = QLabel(tag)
            lbl.setObjectName("value")
            color_row.addWidget(lbl)
            color_row.addWidget(cb)
            color_row.addSpacing(4)
        color_row.addStretch(1)
        f1.addRow("文字颜色", color_row)
        # 预览联动（放在色块创建之后，避免回调时控件还没建好）
        self.font_size.valueChanged.connect(self._refresh_preview)
        self.opacity.valueChanged.connect(self._on_opacity_changed)
        for cb in (self.teacher_color, self.me_color, self.zh_color):
            cb.changed.connect(self._refresh_preview)
        tabs.addTab(_scroll_page(show), "显示")

        # ---- 设备 ----
        dev = QWidget()
        f2 = QFormLayout(dev)
        from .audio import list_loopback_devices, list_mic_devices

        # 第一项固定为"自动"：跟随系统默认输出/输入设备（设备改名/增删后不会失联）
        self.cmb_loopback = QComboBox()
        loopbacks = list_loopback_devices()
        self.cmb_loopback.addItem(_AUTO_DEVICE_LABEL)
        if loopbacks:
            self.cmb_loopback.addItems(loopbacks)
        else:
            self.cmb_loopback.addItem("（未发现回环设备）")
        cur = cfg.get("audio", "loopback_device") or "auto"
        if cur.lower() == "auto":
            self.cmb_loopback.setCurrentIndex(0)
        elif cur in loopbacks:
            self.cmb_loopback.setCurrentText(cur)
        else:
            self.cmb_loopback.addItem(f"{cur}（当前不存在，保存后自动回退）")
            self.cmb_loopback.setCurrentText(f"{cur}（当前不存在，保存后自动回退）")
        f2.addRow("回环设备(老师)", self.cmb_loopback)

        self.cmb_mic = QComboBox()
        mics = list_mic_devices()
        self.cmb_mic.addItem(_AUTO_DEVICE_LABEL)
        if mics:
            self.cmb_mic.addItems(mics)
        else:
            self.cmb_mic.addItem("（未发现麦克风）")
        cur = cfg.get("audio", "mic_device") or "auto"
        if cur.lower() == "auto":
            self.cmb_mic.setCurrentIndex(0)
        elif cur in mics:
            self.cmb_mic.setCurrentText(cur)
        else:
            self.cmb_mic.addItem(f"{cur}（当前不存在，保存后自动回退）")
            self.cmb_mic.setCurrentText(f"{cur}（当前不存在，保存后自动回退）")
        f2.addRow("麦克风设备(我)", self.cmb_mic)
        tip = QLabel("提示：老师声音走「回环」；若默认麦克风是虚拟声卡（如 ToDesk），"
                     "会混入系统播放声，请选真实物理麦克风。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#93A0BF;font-size:11px;")
        f2.addRow(tip)
        tabs.addTab(_scroll_page(dev), "设备")

        # ---- 输出 ----
        out = QWidget()
        f3 = QFormLayout(out)
        dir_row = QHBoxLayout()
        self.edt_output = QLineEdit(str(cfg.resolve_path(cfg.get("session", "output_dir"))))
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse)
        dir_row.addWidget(self.edt_output, 1)
        dir_row.addWidget(btn_browse)
        f3.addRow("输出目录", dir_row)
        self.chk_summary = QCheckBox("结束会话后自动生成纪要（默认关闭；关闭时点工具栏「纪要」手动生成）")
        self.chk_summary.setChecked(cfg.get_bool("session", "auto_summary"))
        f3.addRow(self.chk_summary)
        f3.addRow(QLabel("产物：录音 WAV ×2、SRT/VTT/TXT 转写、summary.md 纪要"))
        tabs.addTab(_scroll_page(out), "输出")

        # ---- 语言 ----
        lang = QWidget()
        fl = QFormLayout(lang)
        self.cmb_source_lang = QComboBox()
        for code, name in [("ru", "俄语"), ("zh", "中文"), ("en", "英语"), ("auto", "自动检测")]:
            self.cmb_source_lang.addItem(name, code)
        cur_src = cfg.get("language", "source_lang")
        idx = self.cmb_source_lang.findData(cur_src)
        if idx >= 0:
            self.cmb_source_lang.setCurrentIndex(idx)
        fl.addRow("源语言（识别/课堂语言）", self.cmb_source_lang)

        self.cmb_target_lang = QComboBox()
        for code, name in [("zh", "中文"), ("en", "英语"), ("ru", "俄语"), ("none", "不翻译（仅显示原文）")]:
            self.cmb_target_lang.addItem(name, code)
        cur_tgt = cfg.get("language", "target_lang")
        idx = self.cmb_target_lang.findData(cur_tgt)
        if idx >= 0:
            self.cmb_target_lang.setCurrentIndex(idx)
        fl.addRow("目标语言（翻译语言）", self.cmb_target_lang)
        lang_tip = QLabel("源语言是网课老师说的语言；目标语言是你想看的字幕语言。"
                           "本地 Whisper 和在线识别都读这个配置。")
        lang_tip.setWordWrap(True)
        lang_tip.setStyleSheet("color:#93A0BF;font-size:11px;")
        fl.addRow(lang_tip)
        tabs.addTab(_scroll_page(lang), "语言")

        # ---- AI 模型：识别服务 / 翻译服务 两套独立配置 ----
        ai = QWidget()
        fa = QFormLayout(ai)

        svc_tip = QLabel("识别与翻译可以是完全不同的服务商：各自的地址、各自的 API Key，互不影响。")
        svc_tip.setWordWrap(True)
        svc_tip.setStyleSheet("color:#8AB4FF;font-size:11px;")
        fa.addRow(svc_tip)

        # ---- 识别服务（语音大模型）----
        fa.addRow(_group_label("识别服务（语音 → 文本）"))
        # ASR 引擎选择
        self.cmb_asr_engine = QComboBox()
        self.cmb_asr_engine.addItems(["aliyun", "yandex"])
        self.cmb_asr_engine.setCurrentText(cfg.get("asr", "engine", fallback="aliyun"))
        self.cmb_asr_engine.currentTextChanged.connect(self._on_asr_engine_changed)
        fa.addRow("识别引擎", self.cmb_asr_engine)
        # 阿里云配置容器
        self._aliyun_box = QWidget()
        aliyun_layout = QVBoxLayout(self._aliyun_box)
        aliyun_layout.setContentsMargins(0, 0, 0, 0)
        self.edt_asr_url = QLineEdit(cfg.asr_base_url())
        self.edt_asr_url.setPlaceholderText("wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        aliyun_layout.addWidget(self.edt_asr_url)
        self.edt_asr_key = QLineEdit(cfg.raw("asr", "api_key"))
        self.edt_asr_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edt_asr_key.setPlaceholderText("留空 = 复用翻译服务的 Key")
        aliyun_layout.addWidget(self.edt_asr_key)
        self.lbl_asr_key = QLabel(f"当前生效：{cfg.asr_key_source()}")
        self.lbl_asr_key.setStyleSheet("color:#93A0BF;font-size:11px;")
        aliyun_layout.addWidget(self.lbl_asr_key)
        self.edt_asr_model = QLineEdit(cfg.get("asr", "model"))
        self.edt_asr_model.setPlaceholderText("如 qwen-audio-3.0-asr-flash-streaming")
        aliyun_layout.addWidget(self.edt_asr_model)
        fa.addRow("阿里云配置", self._aliyun_box)
        # Yandex 配置容器
        self._yandex_box = QWidget()
        yandex_layout = QVBoxLayout(self._yandex_box)
        yandex_layout.setContentsMargins(0, 0, 0, 0)
        self.edt_yandex_key = QLineEdit(cfg.get("yandex", "api_key", fallback=""))
        self.edt_yandex_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edt_yandex_key.setPlaceholderText("Yandex Cloud 服务账号 API Key")
        yandex_layout.addWidget(self.edt_yandex_key)
        yandex_tip = QLabel("俄罗斯本地服务，从俄罗斯访问稳定。\n"
                            "注册：Yandex Cloud → 服务账号 → 分配 ai.speechkit-stt.user 角色 → 获取 API Key。\n"
                            "地址：stt.api.cloud.yandex.net:443（gRPC，无需手动填写）")
        yandex_tip.setWordWrap(True)
        yandex_tip.setStyleSheet("color:#93A0BF;font-size:11px;")
        yandex_layout.addWidget(yandex_tip)
        fa.addRow("Yandex 配置", self._yandex_box)
        self.btn_test_asr = QPushButton("测试识别服务")
        self.btn_test_asr.clicked.connect(self._test_asr)
        fa.addRow(self.btn_test_asr)
        asr_tip = QLabel("aliyun：阿里云百炼 WebSocket 流式识别（国内稳定，俄罗斯访问可能不稳定）。\n"
                         "yandex：Yandex SpeechKit gRPC 流式识别（俄罗斯本地，从俄罗斯访问稳定）。")
        asr_tip.setWordWrap(True)
        asr_tip.setStyleSheet("color:#93A0BF;font-size:11px;")
        fa.addRow(asr_tip)
        self._on_asr_engine_changed(self.cmb_asr_engine.currentText())

        # ---- 翻译服务（翻译大模型）----
        fa.addRow(_group_label("翻译服务（文本 → 译文）"))
        self.edt_base = QLineEdit(cfg.translate_base_url())
        fa.addRow("翻译服务地址", self.edt_base)
        self.edt_key = QLineEdit(cfg.raw("translate", "api_key"))
        self.edt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edt_key.setPlaceholderText("仅 OpenAI 兼容引擎需要")
        fa.addRow("翻译服务 API Key", self.edt_key)
        # 翻译引擎
        self.cmb_engine = QComboBox()
        self.cmb_engine.addItems(["ollama", "openai"])
        self.cmb_engine.setCurrentText(cfg.get("translate", "engine"))
        self.cmb_engine.currentTextChanged.connect(self._on_engine_changed)
        fa.addRow("翻译引擎", self.cmb_engine)
        # 翻译模型（Ollama 动态拉取 + 可编辑）
        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        self._reload_models()
        fa.addRow("翻译模型", self.cmb_model)
        self.edt_fallback = QLineEdit(cfg.get("translate", "fallback_model_name"))
        fa.addRow("备用模型(主模型不可用时)", self.edt_fallback)
        model_tip = QLabel("实测延迟：qwen-mt-turbo 555ms ＜ qwen-flash 714ms ＜ qwen-turbo 856ms "
                           "＜ qwen-max 2.2s。\n"
                           "注意：只有 qwen-max、qwen-turbo 会执行「识别纠错」；"
                           "qwen-flash / qwen-plus / qwen3-max 会忽略该指令。")
        model_tip.setWordWrap(True)
        model_tip.setStyleSheet("color:#93A0BF;font-size:11px;")
        fa.addRow(model_tip)
        self.btn_test_tr = QPushButton("测试翻译服务")
        self.btn_test_tr.clicked.connect(self._test_translate)
        fa.addRow(self.btn_test_tr)

        # ---- 提示词相关 ----
        fa.addRow(_group_label("提示词与术语"))
        self.edt_topic = QLineEdit(cfg.get("translate", "topic"))
        self.edt_topic.setPlaceholderText("如：高等数学 / 经济学原理（提升专业术语翻译）")
        fa.addRow("课程主题", self.edt_topic)
        self.edt_glossary = QLineEdit(cfg.get("translate", "glossary"))
        self.edt_glossary.setPlaceholderText("俄语=中文；多项用分号分隔，如 производная=导数")
        fa.addRow("术语表", self.edt_glossary)
        self.chk_correct = QCheckBox("启用识别纠错（AI 发现识别错误时纠正俄语原文，字幕显示纠正后文本）")
        self.chk_correct.setChecked(cfg.get_bool("translate", "auto_correct"))
        fa.addRow("识别纠错", self.chk_correct)

        # ---- 延迟调优（影响"说完到出字幕"的体感延迟）----
        fa.addRow(_group_label("延迟调优"))
        self.sp_silence = QSpinBox()
        self.sp_silence.setRange(300, 4000)
        self.sp_silence.setSingleStep(100)
        self.sp_silence.setSuffix(" ms")
        self.sp_silence.setValue(cfg.get_int("asr", "max_sentence_silence"))
        fa.addRow("出句静音阈值(越小越快出句)", self.sp_silence)

        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(1, 4)
        self.sp_workers.setValue(cfg.get_int("translate", "workers"))
        fa.addRow("翻译并发句数", self.sp_workers)

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(5, 120)
        self.sp_timeout.setSuffix(" s")
        self.sp_timeout.setValue(cfg.get_int("translate", "timeout_sec"))
        fa.addRow("单句翻译超时", self.sp_timeout)

        self.sp_context = QSpinBox()
        self.sp_context.setRange(0, 10)
        self.sp_context.setValue(cfg.get_int("translate", "context_sentences"))
        fa.addRow("携带上文字数(术语一致)", self.sp_context)

        cut_tip = QLabel("静音阈值：说完整句后等多久算一句结束（越小出字幕越快，但更容易断句）。\n"
                         "翻译并发：同时翻几句；语速快时调到 2~3 可避免积压（字幕顺序不变）。")
        cut_tip.setWordWrap(True)
        cut_tip.setStyleSheet("color:#93A0BF;font-size:11px;")
        fa.addRow(cut_tip)
        tabs.addTab(_scroll_page(ai), "AI 模型")


        self.body.addWidget(tabs, 1)

        # 底部按钮：主次分明，中文文案（Qt 默认是 OK / Cancel）
        row = QHBoxLayout()
        row.setSpacing(10)
        btn_cancel = QPushButton("取消")
        btn_ok = QPushButton("保存")
        btn_ok.setObjectName("primary")
        btn_ok.setDefault(True)
        btn_ok.setMinimumWidth(96)
        btn_cancel.setMinimumWidth(80)
        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self._apply)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        self.body.addLayout(row)
        self.resize(660, 640)
        self._refresh_preview()

    # ---- 显示页：实时预览 ----
    def _on_opacity_changed(self, value: int) -> None:
        self.lbl_opacity.setText(f"{value}%")
        self._refresh_preview()

    def _on_skin_changed(self, _index: int) -> None:
        self.preview.set_skin(self.skin_cb.currentData())

    def _refresh_preview(self) -> None:
        try:
            self.preview.set_values(self.font_size.value(), self.opacity.value(),
                                    self.teacher_color.hex(), self.zh_color.hex())
        except AttributeError:  # 初始化过程中还没建完控件
            pass

    # ---- 服务连通性自检（不用等上课才发现 Key/地址写错）----
    def _test_asr(self) -> None:
        engine = self.cmb_asr_engine.currentText()
        if engine == "yandex":
            yandex_key = self.edt_yandex_key.text().strip()
            self._start_test(self.btn_test_asr, self._run_yandex_test, yandex_key)
        else:
            url = self.edt_asr_url.text().strip()
            key = self.edt_asr_key.text().strip() or self._cfg.asr_api_key()
            model = self.edt_asr_model.text().strip()
            self._start_test(self.btn_test_asr, self._run_asr_test, url, key, model)

    def _test_translate(self) -> None:
        base = self.edt_base.text().strip()
        key = self.edt_key.text().strip() or self._cfg.translate_api_key()
        model = self.cmb_model.currentText().strip()
        engine = self.cmb_engine.currentText()
        self._start_test(self.btn_test_tr, self._run_translate_test, base, key, model, engine)

    def _start_test(self, btn, fn, *args) -> None:
        import threading
        if getattr(btn, "_busy", False):
            return
        btn._busy = True
        old = btn.text()
        btn.setText("测试中…")
        btn.setEnabled(False)

        def _work():
            try:
                msg, ok = fn(*args)
            except Exception as exc:  # noqa: BLE001
                msg, ok = f"测试异常：{exc}", False
            self.test_done.emit(f"{msg}\n\n（{old}）", ok)

        threading.Thread(target=_work, daemon=True).start()

    def _on_test_done(self, msg: str, ok: bool) -> None:
        from PyQt6.QtWidgets import QMessageBox, QTextEdit, QVBoxLayout, QDialog, QDialogButtonBox
        from PyQt6.QtCore import Qt
        for btn in (self.btn_test_asr, self.btn_test_tr):
            if getattr(btn, "_busy", False):
                btn._busy = False
                btn.setEnabled(True)
                btn.setText("测试识别服务" if btn is self.btn_test_asr else "测试翻译服务")
        # 写入日志文件，方便查看完整报错
        try:
            import time as _t
            log_path = self._cfg.resolve_path("service_test.log")
            with open(log_path, "a", encoding="utf-8") as _f:
                _f.write(f"\n[{_t.strftime('%Y-%m-%d %H:%M:%S')}] {'成功' if ok else '失败'}\n{msg}\n")
        except Exception:
            pass
        # 使用可滚动的玻璃对话框显示完整报错
        dlg = theme.GlassDialog("服务测试结果", self)
        dlg.set_subtitle("识别 / 翻译服务连通性")
        dlg.resize(680, 500)
        layout = dlg.body
        title = "✅ 测试成功" if ok else "❌ 测试失败"
        from PyQt6.QtWidgets import QLabel
        lbl = QLabel(title)
        lbl.setStyleSheet("font-size:16px;font-weight:600;color:"
                          + ("#5BE3A6" if ok else "#FF8C8C") + ";")
        layout.addWidget(lbl)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(msg)
        text.setStyleSheet("font-family:Consolas,monospace;font-size:12px;")
        layout.addWidget(text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        ok_btn = QPushButton("关闭")
        ok_btn.setObjectName("primary")
        ok_btn.setMinimumWidth(90)
        ok_btn.clicked.connect(dlg.accept)
        row.addWidget(ok_btn)
        layout.addLayout(row)
        dlg.exec()

    @staticmethod
    def _run_asr_test(url: str, key: str, model: str) -> tuple[str, bool]:
        """连一次识别 WebSocket，等到 task-started 即视为可用。"""
        import json
        import threading as _th
        import time as _t
        import uuid
        if not url or not key:
            return "识别服务地址或 API Key 为空", False
        try:
            import websocket
        except ImportError:
            return "缺少 websocket-client 库", False
        started = _th.Event()
        err: dict[str, str] = {}
        close_info: dict[str, str] = {}
        t0 = _t.time()
        run_task = {
            "header": {"action": "run-task", "task_id": str(uuid.uuid4()), "streaming": "duplex"},
            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                        "model": model, "input": {},
                        "parameters": {"format": "pcm", "sample_rate": 16000,
                                       "language_hints": ["ru"], "max_sentence_silence": 1300}},
        }
        payload_str = json.dumps(run_task, ensure_ascii=False)

        def on_open(ws):
            ws.send(payload_str)

        def on_message(ws, message):
            try:
                ev = json.loads(message).get("header", {}).get("event", "")
            except Exception:  # noqa: BLE001
                return
            if ev == "task-started":
                started.set()
            elif ev == "error":
                err["msg"] = message[:500]

        def on_error(ws, e):
            err.setdefault("msg", str(e)[:500])

        def on_close(ws, code, msg):
            close_info["code"] = str(code)
            close_info["msg"] = str(msg)[:300]

        ws = websocket.WebSocketApp(url, header={"Authorization": f"Bearer {key}"},
                                    on_open=on_open, on_message=on_message,
                                    on_error=on_error, on_close=on_close)
        _th.Thread(target=ws.run_forever, daemon=True).start()
        ok = started.wait(10)
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        cost = int((_t.time() - t0) * 1000)
        if ok:
            return f"识别服务连接成功（{cost}ms）\n地址：{url}\n模型：{model}", True
        parts = []
        if err.get("msg"):
            parts.append(f"错误：{err['msg']}")
        if close_info.get("code"):
            parts.append(f"连接关闭码：{close_info['code']}，原因：{close_info.get('msg', '')}")
        parts.append(f"发送的 payload：{payload_str}")
        if parts:
            return "识别服务返回错误：\n" + "\n".join(parts), False
        return "识别服务连接超时（10 秒）——请检查地址、API Key 与网络", False

    @staticmethod
    def _run_yandex_test(api_key: str) -> tuple[str, bool]:
        """测试 Yandex SpeechKit gRPC 连接：建立通道 + 发送 session_options，等待响应。"""
        import time as _t
        if not api_key:
            return "Yandex API Key 为空，请在设置中填写", False
        try:
            import sys
            import os
            proto_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yandex_proto")
            if proto_dir not in sys.path:
                sys.path.insert(0, proto_dir)
            import grpc
            from google.protobuf import descriptor as _pb  # noqa: F401
            from yandex.cloud.ai.stt.v3 import stt_pb2
            from yandex.cloud.ai.stt.v3 import stt_service_pb2_grpc
        except Exception as exc:  # noqa: BLE001
            return f"Yandex 依赖导入失败：{exc}", False
        t0 = _t.time()
        try:
            cred = grpc.ssl_channel_credentials()
            channel = grpc.secure_channel("stt.api.cloud.yandex.net:443", cred)
            stub = stt_service_pb2_grpc.RecognizerStub(channel)
            # 构造 session_options（第一条消息）
            session_options = stt_pb2.StreamingOptions(
                recognition_model=stt_pb2.RecognitionModelOptions(
                    audio_format=stt_pb2.AudioFormatOptions(
                        raw_audio=stt_pb2.RawAudio(
                            audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
                            sample_rate_hertz=16000,
                            audio_channel_count=1,
                        )
                    ),
                    text_normalization=stt_pb2.TextNormalizationOptions(
                        text_normalization=stt_pb2.TextNormalizationOptions.TEXT_NORMALIZATION_ENABLED,
                    ),
                    language_restriction=stt_pb2.LanguageRestrictionOptions(
                        restriction_type=stt_pb2.LanguageRestrictionOptions.WHITELIST,
                        language_code=["ru-RU"],
                    ),
                )
            )
            config_msg = stt_pb2.StreamingRequest(session_options=session_options)
            def _gen():
                yield config_msg
            call = stub.RecognizeStreaming(
                _gen(),
                metadata=[("authorization", f"Api-Key {api_key}")],
            )
            # 等待第一条响应或错误（最多5秒）
            import threading as _th
            result = {}
            def _recv():
                try:
                    for resp in call:
                        result["resp"] = str(resp)[:200]
                        break
                except Exception as exc:  # noqa: BLE001
                    result["err"] = str(exc)[:300]
            t = _th.Thread(target=_recv, daemon=True)
            t.start()
            t.join(timeout=5)
            call.cancel()
            channel.close()
            cost = int((_t.time() - t0) * 1000)
            if "err" in result:
                return f"Yandex 连接失败（{cost}ms）\n错误：{result['err']}\n地址：stt.api.cloud.yandex.net:443", False
            if "resp" in result:
                return f"Yandex SpeechKit 连接成功（{cost}ms）\n地址：stt.api.cloud.yandex.net:443\n语言：ru-RU", True
            return f"Yandex 连接超时（5秒无响应）\n地址：stt.api.cloud.yandex.net:443", False
        except Exception as exc:  # noqa: BLE001
            cost = int((_t.time() - t0) * 1000)
            return f"Yandex 连接异常（{cost}ms）：{exc}", False

    def _run_translate_test(self, base: str, key: str, model: str, engine: str) -> tuple[str, bool]:
        """发一次最小翻译请求，验证地址/Key/模型三者可用。"""
        import time as _t
        import requests
        if not base or not model:
            return "翻译服务地址或模型为空", False
        t0 = _t.time()
        try:
            if engine == "openai":
                headers = {"Content-Type": "application/json"}
                if key:
                    headers["Authorization"] = f"Bearer {key}"
                resp = requests.post(
                    f"{base.rstrip('/')}/v1/chat/completions", headers=headers,
                    json={"model": model, "max_tokens": 16,
                          "messages": [{"role": "user", "content": "回复：可用"}]},
                    timeout=20)
            else:
                resp = requests.post(
                    f"{base.rstrip('/')}/api/chat",
                    json={"model": model, "stream": False, "messages": [{"role": "user", "content": "回复：可用"}]},
                    timeout=20)
        except requests.exceptions.RequestException as exc:
            return f"翻译服务连接失败：{exc}", False
        cost = int((_t.time() - t0) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                text = (data["choices"][0]["message"]["content"] if engine == "openai"
                        else (data.get("message") or {}).get("content", "")) or ""
            except Exception:  # noqa: BLE001
                text = ""
            return f"翻译服务可用（{cost}ms）\n模型：{model}\n返回：{(text or '(空)')[:40]}", True
        detail = ""
        try:
            detail = str(resp.json().get("error", resp.text))[:200]
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:200]
        hint = {401: "API Key 无效", 403: "无权限或免费额度用尽",
                404: "模型不存在或地址路径错误"}.get(resp.status_code, "")
        return f"翻译服务返回 HTTP {resp.status_code} {hint}\n{detail}", False

    def _reload_models(self) -> None:
        from .translator import list_ollama_models

        base = self._cfg.translate_base_url()
        if self._cfg.get("translate", "engine") == "ollama":
            models = list_ollama_models(base)
            if models:
                self.cmb_model.clear()
                self.cmb_model.addItems(models)
                cur = self._cfg.get("translate", "model_name")
                if cur in models:
                    self.cmb_model.setCurrentText(cur)
        self.cmb_model.setEditText(self._cfg.get("translate", "model_name"))

    def _on_engine_changed(self, engine: str) -> None:
        if engine == "ollama":
            self._reload_models()
        else:
            self.cmb_model.setEditText(self._cfg.get("translate", "model_name"))

    def _on_asr_engine_changed(self, engine: str) -> None:
        """根据选择的 ASR 引擎显示/隐藏对应配置。"""
        is_yandex = engine == "yandex"
        self._aliyun_box.setVisible(not is_yandex)
        self._yandex_box.setVisible(is_yandex)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录",
                                             self.edt_output.text())
        if d:
            self.edt_output.setText(d)

    def _apply(self) -> None:
        c = self._cfg
        c.set("ui", "font_size", str(self.font_size.value()))
        c.set("ui", "opacity", str(self.opacity.value()))
        c.set("ui", "skin", self.skin_cb.currentData())
        c.set("ui", "caption_segments", str(self.caption_segments.value()))
        c.set("ui", "caption_text_color", self.caption_color_cb.currentData())
        c.set("ui", "history_lines", str(self.history.value()))
        c.set("ui", "auto_hide_minutes", str(self.auto_hide.currentData()))
        c.set("ui", "teacher_color", self.teacher_color.hex())
        c.set("ui", "me_color", self.me_color.hex())
        c.set("ui", "text_color", self.zh_color.hex())
        # 设备：'自动' 写回 auto；选了当前不存在的设备也回退成 auto，避免再次失联
        lb = self.cmb_loopback.currentText()
        mic = self.cmb_mic.currentText()
        if lb == _AUTO_DEVICE_LABEL or "不存在" in lb:
            c.set("audio", "loopback_device", "auto")
        elif "未发现" not in lb:
            c.set("audio", "loopback_device", lb)
        if mic == _AUTO_DEVICE_LABEL or "不存在" in mic:
            c.set("audio", "mic_device", "auto")
        elif "未发现" not in mic:
            c.set("audio", "mic_device", mic)
        c.set("session", "output_dir", self.edt_output.text())
        c.set("session", "auto_summary",
              "true" if self.chk_summary.isChecked() else "false")
        # 语言页
        c.set("language", "source_lang", self.cmb_source_lang.currentData())
        c.set("language", "target_lang", self.cmb_target_lang.currentData())
        # AI 模型页：识别服务与翻译服务各自独立保存
        c.set("asr", "engine", self.cmb_asr_engine.currentText())
        c.set("asr", "ws_url", self.edt_asr_url.text().strip())
        c.set("asr", "api_key", self.edt_asr_key.text().strip())
        c.set("asr", "model", self.edt_asr_model.text().strip())
        c.set("yandex", "api_key", self.edt_yandex_key.text().strip())
        c.set("asr", "max_sentence_silence", str(self.sp_silence.value()))
        c.set("translate", "workers", str(self.sp_workers.value()))
        c.set("translate", "timeout_sec", str(self.sp_timeout.value()))
        c.set("translate", "context_sentences", str(self.sp_context.value()))
        c.set("translate", "engine", self.cmb_engine.currentText())
        c.set("translate", "model_name", self.cmb_model.currentText().strip())
        c.set("translate", "fallback_model_name", self.edt_fallback.text().strip())
        c.set("translate", "base_url", self.edt_base.text().strip())
        c.set("translate", "api_key", self.edt_key.text().strip())
        c.set("translate", "topic", self.edt_topic.text().strip())
        c.set("translate", "glossary", self.edt_glossary.text().strip())
        c.set("translate", "auto_correct",
              "true" if self.chk_correct.isChecked() else "false")
        # 皮肤/段数立即生效，不用重启
        try:
            theme.set_skin(self.skin_cb.currentData())
            parent = self.parent()
            if parent is not None:
                parent.update()
                if hasattr(parent, "_refresh_caption_color"):
                    parent._refresh_caption_color()
                parent._refresh_text()
        except Exception:  # noqa: BLE001
            pass
        self.accept()
