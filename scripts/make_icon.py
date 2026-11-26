#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「项目伴侣」图标：App 图标（png/icns）+ 菜单栏模板图。

视觉概念「晾衣绳」：粉蓝底、白绳晾着红黄绿三份文件，木夹标记最新那份，
呼应项目代号 project-companion。

运行：python3 scripts/make_icon.py
产出：assets/ 下 menu_bar_template*.png、icon_1024.png、AppIcon.icns
"""
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

AMBER = (222, 155, 74, 255)   # 暖琥珀
INK = (34, 29, 23, 255)       # 墨色（木夹）
WHITE = (255, 255, 255, 255)

SKY = (169, 215, 232, 255)    # 粉蓝底
RED = (224, 90, 78, 255)      # 红
YELLOW = (242, 179, 61, 255)  # 黄
GREEN = (79, 162, 107, 255)   # 绿

SS = 4  # 超采样倍率，最后缩小抗锯齿


def bezier(p0, p1, p2, p3, steps=120):
    """三次贝塞尔曲线采样点。"""
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def hanging_item(size, sheet, lines, peg, outline=None, outline_width=14):
    """单个「文件 + 木夹」图层。坐标均为 1024 母版坐标系，内部按图层尺寸换算。

    sheet: (x, y, w, h, 圆角, 填充色)；lines: [(x, y, w, h, 圆角, 颜色)]；
    peg: (x, y, w, h, 圆角)；outline: 文件描边色；outline_width: 描边宽度（1024 坐标系）
    """
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = size / 1024

    def rect(v):
        x, y, w, h = v[:4]
        return [int(x * f), int(y * f), int((x + w) * f), int((y + h) * f)], int(v[4] * f)

    r, rad = rect(sheet)
    d.rounded_rectangle(r, radius=rad, fill=sheet[5],
                        outline=outline, width=int(outline_width * f) if outline else 0)
    for line in lines:
        r, rad = rect(line)
        d.rounded_rectangle(r, radius=rad, fill=line[5])
    r, rad = rect(peg)
    d.rounded_rectangle(r, radius=rad, fill=peg[5])
    return layer


def draw_app_icon(size):
    k = SS
    S = size * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=SKY)

    # 晾衣绳：从左上到右上、中间下垂
    rope = bezier((40, 310), (320, 468), (704, 468), (984, 310))
    rope = [(x * k, y * k) for x, y in rope]
    rw = int(28 * k)
    d.line(rope, fill=WHITE, width=rw, joint="curve")
    for cap in (rope[0], rope[-1]):
        r = rw // 2
        d.ellipse([cap[0] - r, cap[1] - r, cap[0] + r, cap[1] + r], fill=WHITE)

    soft_white = (255, 255, 255, 217)

    # 左：红文件（最新版，最大）
    left = hanging_item(
        S,
        sheet=(150, 445, 250, 330, 20, RED),
        lines=[(192, 530, 150, 24, 12, WHITE), (192, 585, 100, 24, 12, soft_white),
               (192, 640, 130, 24, 12, soft_white)],
        peg=(251, 392, 48, 70, 16, INK))
    img.alpha_composite(left.rotate(4, center=(275 * k, 610 * k), resample=Image.BICUBIC))

    # 中：黄文件
    mid = hanging_item(
        S,
        sheet=(445, 445, 210, 285, 17, YELLOW),
        lines=[(477, 520, 126, 22, 11, WHITE), (477, 568, 84, 22, 11, soft_white),
               (477, 616, 105, 22, 11, soft_white)],
        peg=(526, 414, 48, 64, 16, INK))
    img.alpha_composite(mid.rotate(-2, center=(550 * k, 610 * k), resample=Image.BICUBIC))

    # 右：绿文件（最小）
    right = hanging_item(
        S,
        sheet=(700, 445, 175, 240, 15, GREEN),
        lines=[(728, 510, 104, 20, 10, WHITE), (728, 552, 70, 20, 10, soft_white),
               (728, 594, 88, 20, 10, soft_white)],
        peg=(764, 384, 48, 70, 16, INK))
    img.alpha_composite(right.rotate(-5, center=(788 * k, 610 * k), resample=Image.BICUBIC))

    return img.resize((size, size), Image.LANCZOS)


def draw_menu_icon(size):
    """菜单栏模板图：墨色晾衣绳（纯黑 + 透明底，macOS 自动反色）。"""
    k = 8  # 超采样
    S = size * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = lambda v: v * S

    rope = bezier((u(0.06), u(0.26)), (u(0.36), u(0.42)), (u(0.64), u(0.42)), (u(0.94), u(0.26)))
    d.line(rope, fill=INK, width=int(S * 0.09), joint="curve")

    # 三份文件并排挂着（与 App 图标的三文件概念一致；18pt 下不放文字槽和木夹）
    d.rounded_rectangle([u(0.12), u(0.38), u(0.32), u(0.78)], radius=u(0.03), fill=INK)
    d.rounded_rectangle([u(0.40), u(0.38), u(0.60), u(0.72)], radius=u(0.03), fill=INK)
    d.rounded_rectangle([u(0.68), u(0.38), u(0.88), u(0.66)], radius=u(0.03), fill=INK)

    return img.resize((size, size), Image.LANCZOS)


def main():
    # 菜单栏模板图（墨色晾衣绳，透明底）
    draw_menu_icon(18).save(ASSETS / "menu_bar_template.png")
    draw_menu_icon(36).save(ASSETS / "menu_bar_template@2x.png")

    # App 图标母版
    master = draw_app_icon(1024)
    master.save(ASSETS / "icon_1024.png")

    # iconset → icns
    iconset = ASSETS / "AppIcon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for s in (16, 32, 64, 128, 256, 512, 1024):
        master.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
        master.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "AppIcon.icns")],
                   check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    print("icon done ->", ASSETS)


if __name__ == "__main__":
    main()
