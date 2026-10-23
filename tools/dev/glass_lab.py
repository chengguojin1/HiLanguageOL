# -*- coding: utf-8 -*-
r"""液态玻璃皮肤预览：在彩色壁纸上渲染 3 套字幕窗皮肤，输出对比图。

只用于选型预览，不依赖 app 内任何模块，不影响现有程序。
运行：.venv\Scripts\python.exe tools\dev\glass_lab.py
输出：tools\dev\out\glass_variants.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")  # 需要系统字体，offscreen 插件拿不到

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from PyQt6.QtCore import QRectF, Qt, QPointF
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QGuiApplication, QImage, QLinearGradient,
    QPainter, QPainterPath, QPen, QRadialGradient,
)

W, H = 1040, 360          # 单个方案画布
PANEL = QRectF(120, 118, 760, 190)
TOOLBAR = QRectF(120, 66, 372, 42)

EN = "So the key idea is that the gradient of the loss tells us which direction to move."
ZH = "所以关键在于：损失的梯度告诉我们应该往哪个方向移动。"


# ---------------------------------------------------------------- 壁纸
def wallpaper(w: int, h: int) -> QImage:
    """模拟桌面壁纸：深色底 + 彩色光斑，用来检验玻璃的取景效果。"""
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(0x0B, 0x10, 0x24))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QLinearGradient(0, 0, w, h)
    base.setColorAt(0.0, QColor(0x16, 0x1B, 0x3A))
    base.setColorAt(0.5, QColor(0x0E, 0x2A, 0x3F))
    base.setColorAt(1.0, QColor(0x2A, 0x12, 0x2E))
    p.fillRect(0, 0, w, h, base)
    blobs = [
        (0.18, 0.22, 0.42, QColor(0x4F, 0x7C, 0xFF, 210)),
        (0.72, 0.28, 0.38, QColor(0xFF, 0x6A, 0xC4, 190)),
        (0.55, 0.85, 0.45, QColor(0x2A, 0xE0, 0xC8, 170)),
        (0.92, 0.72, 0.30, QColor(0xFF, 0xB3, 0x5C, 150)),
    ]
    p.setPen(Qt.PenStyle.NoPen)
    for cx, cy, r, col in blobs:
        g = QRadialGradient(QPointF(cx * w, cy * h), r * min(w, h))
        g.setColorAt(0.0, col)
        c2 = QColor(col)
        c2.setAlpha(0)
        g.setColorAt(1.0, c2)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx * w, cy * h), r * min(w, h), r * min(w, h))
    p.end()
    return img


def qimage_to_pil(img: QImage) -> Image.Image:
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    buf = img.constBits()
    buf.setsize(img.sizeInBytes())
    arr = np.frombuffer(buf, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :]
    return Image.fromarray(arr.copy(), "RGBA")


def pil_to_qimage(im: Image.Image) -> QImage:
    im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    q = QImage(data, im.width, im.height, QImage.Format.Format_RGBA8888)
    return q.copy()


def blur_region(src: QImage, rect: QRectF, radius: float, sat: float = 1.0,
                bright: float = 1.0) -> QImage:
    """把壁纸 rect 区域裁出来做高斯模糊（模拟毛玻璃的背板取景）。"""
    crop = src.copy(int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height()))
    im = qimage_to_pil(crop).filter(ImageFilter.GaussianBlur(radius))
    if sat != 1.0:
        im = ImageEnhance.Color(im).enhance(sat)
    if bright != 1.0:
        im = ImageEnhance.Brightness(im).enhance(bright)
    return pil_to_qimage(im)


def noise_layer(w: int, h: int, alpha: int = 9, seed: int = 7) -> QImage:
    """细颗粒噪点：液态玻璃的“材质感”主要靠它，纯色渐变会显得塑料。"""
    rng = np.random.default_rng(seed)
    n = rng.integers(96, 160, (h, w), dtype=np.uint8)
    arr = np.dstack([n, n, n, np.full((h, w), alpha, np.uint8)])
    return pil_to_qimage(Image.fromarray(arr, "RGBA"))


# ---------------------------------------------------------------- 玻璃
VARIANTS = {
    "A · Liquid Glass（亮玻璃）": dict(
        radius=30, blur=26, sat=1.28, bright=1.10,
        tint=[(0.0, QColor(255, 255, 255, 34)), (0.55, QColor(255, 255, 255, 16)),
              (1.0, QColor(255, 255, 255, 30))],
        rim=QColor(255, 255, 255, 205), rim2=QColor(255, 255, 255, 70),
        shadow=150, sheen=46, chroma=True,
        text=QColor(255, 255, 255), sub=QColor(255, 255, 255, 205),
        chip=(QColor(255, 255, 255, 46), QColor(255, 255, 255, 110)),
    ),
    "B · Frosted Dark（暗玻璃，视频上最稳）": dict(
        radius=20, blur=30, sat=1.22, bright=0.86,
        tint=[(0.0, QColor(10, 14, 26, 152)), (0.6, QColor(12, 16, 30, 116)),
              (1.0, QColor(8, 11, 22, 158))],
        rim=QColor(255, 255, 255, 120), rim2=QColor(255, 255, 255, 34),
        shadow=190, sheen=26, chroma=False,
        text=QColor(246, 249, 255), sub=QColor(196, 210, 235),
        chip=(QColor(255, 255, 255, 26), QColor(255, 255, 255, 70)),
    ),
    "C · Aurora（极光玻璃）": dict(
        radius=26, blur=22, sat=1.35, bright=0.94,
        tint=[(0.0, QColor(120, 220, 255, 78)), (0.5, QColor(150, 120, 255, 58)),
              (1.0, QColor(255, 120, 200, 62))],
        rim=QColor(190, 240, 255, 220), rim2=QColor(255, 170, 220, 120),
        shadow=170, sheen=36, chroma=True,
        text=QColor(255, 255, 255), sub=QColor(226, 236, 255, 215),
        chip=(QColor(255, 255, 255, 38), QColor(255, 255, 255, 96)),
    ),
}


def draw_shadow(p: QPainter, rect: QRectF, radius: float, strength: int) -> None:
    """多层外发光近似柔和投影（Qt 的 blur 效果在这种尺寸下更慢）。"""
    for i, (grow, a) in enumerate([(18, 0.10), (12, 0.14), (7, 0.18), (3, 0.22)]):
        c = QColor(0, 0, 0, int(strength * a))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        r = rect.adjusted(-grow, -grow + 6, grow, grow + 10)
        p.drawRoundedRect(r, radius + grow, radius + grow)


def draw_glass(p: QPainter, rect: QRectF, backdrop: QImage, cfg: dict) -> None:
    radius = cfg["radius"]
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    draw_shadow(p, rect, radius, cfg["shadow"])

    p.save()
    p.setClipPath(path)
    # 1) 模糊背板
    p.drawImage(rect.topLeft(), blur_region(backdrop, rect, cfg["blur"],
                                            cfg["sat"], cfg["bright"]))
    # 2) 着色层
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    for pos, col in cfg["tint"]:
        g.setColorAt(pos, col)
    p.fillRect(rect, g)
    # 3) 顶部高光（镜面反射）
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + rect.height() * 0.45))
    sheen.setColorAt(0.0, QColor(255, 255, 255, cfg["sheen"]))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillRect(rect, sheen)
    # 4) 斜向一道细反光
    p.save()
    p.translate(rect.left(), rect.top())
    streak = QLinearGradient(QPointF(0, 0), QPointF(rect.width() * 0.75, rect.height() * 0.55))
    streak.setColorAt(0.0, QColor(255, 255, 255, 0))
    streak.setColorAt(0.42, QColor(255, 255, 255, 0))
    streak.setColorAt(0.5, QColor(255, 255, 255, 26))
    streak.setColorAt(0.58, QColor(255, 255, 255, 0))
    streak.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillRect(QRectF(0, 0, rect.width(), rect.height()), streak)
    p.restore()
    # 5) 颗粒
    p.drawImage(rect.topLeft(), noise_layer(int(rect.width()), int(rect.height())))
    p.restore()

    # 6) 折射边缘：上亮下暗的内描边 + 彩色色散
    rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    rim.setColorAt(0.0, cfg["rim"])
    rim.setColorAt(0.55, cfg["rim2"])
    rim.setColorAt(1.0, cfg["rim2"])
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(rim, 1.4))
    p.drawRoundedRect(rect.adjusted(0.7, 0.7, -0.7, -0.7), radius, radius)
    if cfg["chroma"]:
        p.save()
        p.setOpacity(0.55)
        pen = QPen(QColor(120, 220, 255, 90), 1.0)
        p.setPen(pen)
        p.drawRoundedRect(rect.adjusted(2.2, 2.2, -2.2, -2.2), radius - 2, radius - 2)
        p.setPen(QPen(QColor(255, 120, 200, 70), 1.0))
        p.drawRoundedRect(rect.adjusted(3.4, 3.4, -3.4, -3.4), radius - 3, radius - 3)
        p.restore()


def font(size: float, weight: QFont.Weight = QFont.Weight.Normal, family: str = "Segoe UI") -> QFont:
    f = QFont(family, int(size))
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def draw_content(p: QPainter, rect: QRectF, cfg: dict, backdrop: QImage) -> None:
    # 顶部工具条：独立的小玻璃胶囊（对应你 hover 才出现的按钮条）
    tb = TOOLBAR
    q = QPainterPath()
    q.addRoundedRect(tb, tb.height() / 2, tb.height() / 2)
    p.save()
    p.setClipPath(q)
    p.drawImage(tb.topLeft(), blur_region(backdrop, tb, 18, 1.3, 1.05))
    p.fillRect(tb, QColor(255, 255, 255, 30))
    p.restore()
    p.setPen(QPen(QColor(255, 255, 255, 95), 1.2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(tb.adjusted(0.6, 0.6, -0.6, -0.6), tb.height() / 2, tb.height() / 2)

    p.setFont(font(9.5, QFont.Weight.DemiBold))
    labels = ["开始", "暂停", "录音", "字幕", "设置"]
    x = tb.left() + 14
    for i, lab in enumerate(labels):
        w = 54 if i < 3 else 50
        p.setPen(QColor(255, 255, 255, 225))
        p.drawText(QRectF(x, tb.top(), w, tb.height()),
                   Qt.AlignmentFlag.AlignCenter, lab)
        x += w + 4

    # 语言角标（EN→中）：收敛在面板内，用弱玻璃底
    badge = QRectF(rect.right() - 96, rect.top() + 13, 78, 22)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 26))
    p.drawRoundedRect(badge, 11, 11)
    p.setPen(QPen(QColor(255, 255, 255, 110), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(badge.adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
    p.setFont(font(8.0, QFont.Weight.DemiBold))
    p.setPen(QColor(255, 255, 255, 225))
    p.drawText(badge, Qt.AlignmentFlag.AlignCenter, "EN  →  中文")

    # 文本区压暗：亮色壁纸／视频画面上保证字幕可读
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(rect, cfg["radius"], cfg["radius"])
    p.setClipPath(clip)
    scrim = QRadialGradient(
        QPointF(rect.left() + rect.width() * 0.42, rect.center().y()),
        rect.width() * 0.72)
    scrim.setColorAt(0.0, QColor(0, 0, 0, 78))
    scrim.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(rect, scrim)
    p.restore()

    # 内容
    left = rect.left() + 26
    right = rect.right() - 26
    p.setFont(font(11.5))
    p.setPen(QColor(cfg["sub"]))
    p.drawText(QRectF(left, rect.top() + 26, right - left - 104, 46),
               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                   | Qt.TextFlag.TextWordWrap), EN)

    p.setFont(font(16.5, QFont.Weight.DemiBold, "Microsoft YaHei UI"))
    p.setPen(cfg["text"])
    p.drawText(QRectF(left, rect.top() + 82, right - left, 40),
               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), ZH)

    # 底部状态行
    p.setFont(font(8.5))
    p.setPen(QColor(255, 255, 255, 165))
    p.drawText(QRectF(left, rect.bottom() - 30, 240, 18),
               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
               "●  就绪   ·   老师 有声音   ·   我 有声音")

    # 右下缩放抓手
    p.setPen(QPen(QColor(255, 255, 255, 105), 1.6))
    gx, gy = rect.right() - 22, rect.bottom() - 22
    for i in range(3):
        p.drawLine(int(gx + i * 5), int(gy + 14), int(gx + 14), int(gy + i * 5))


def render_variant(name: str, cfg: dict, backdrop: QImage) -> QImage:
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawImage(0, 0, backdrop)
    # 暗化一点壁纸，让字幕区更聚焦
    p.fillRect(0, 0, W, H, QColor(4, 6, 14, 40))
    draw_glass(p, PANEL, backdrop, cfg)
    draw_content(p, PANEL, cfg, backdrop)
    # 标题
    p.setFont(font(11, QFont.Weight.Bold))
    p.setPen(QColor(255, 255, 255, 210))
    p.drawText(QRectF(24, 18, 700, 22),
               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
    p.end()
    return img


def main() -> None:
    app = QGuiApplication(sys.argv)  # 必须持有引用，否则被回收后字体/画笔全部失效
    out = Path(__file__).parent / "out"
    out.mkdir(parents=True, exist_ok=True)
    backdrop = wallpaper(W, H)
    names = list(VARIANTS)
    imgs = []
    for name in names:
        q = render_variant(name, VARIANTS[name], backdrop)
        imgs.append(q)
        path = out / f"glass_{names.index(name)}.png"
        q.save(str(path))
    sheet = QImage(W, H * len(imgs), QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor(8, 10, 18))
    sp = QPainter(sheet)
    for i, im in enumerate(imgs):
        sp.drawImage(0, i * H, im)
    sp.end()
    sheet.save(str(out / "glass_variants.png"))
    print("saved", out)


if __name__ == "__main__":
    main()
