# -*- coding: utf-8 -*-
"""生成托盘状态图标(纯标准库,无 Pillow)。

设计:深炭圆盘 + 暖白细环 + 状态色圆心。16px 下仍能分辨,亮/暗任务栏都可见。
    python desktop/make_icons.py
"""
import math
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")

INK = (23, 27, 29)
CREAM = (240, 240, 236)
STATES = {
    "run": (143, 178, 155),      # sage
    "pause": (217, 164, 92),     # amber
    "error": (212, 108, 88),     # rust
}
SS = 4  # 超采样倍数


def _dist(x, y, cx, cy):
    return math.hypot(x - cx, y - cy)


def render_rgba(size, dot):
    """返回 size×size 的 RGBA 行列表(自上而下)。"""
    n = size * SS
    cx = cy = (n - 1) / 2.0
    r_out = 0.455 * n
    r_ring = 0.285 * n
    w_ring = 0.075 * n
    r_dot = 0.125 * n
    buf = []
    for py in range(n):
        row = []
        for px in range(n):
            d = _dist(px, py, cx, cy)
            if d <= r_out:
                if d > r_out - 0.055 * n:
                    rgb = CREAM
                elif abs(d - r_ring) <= w_ring / 2.0:
                    rgb = CREAM
                elif d <= r_dot:
                    rgb = dot
                else:
                    rgb = INK
                row.append((rgb[0], rgb[1], rgb[2], 255))
            else:
                row.append((0, 0, 0, 0))
        buf.append(row)
    # 盒式降采样
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            r = g = b = a = 0
            for yy in range(SS):
                for xx in range(SS):
                    p = buf[y * SS + yy][x * SS + xx]
                    r += p[0] * p[3]
                    g += p[1] * p[3]
                    b += p[2] * p[3]
                    a += p[3]
            if a:
                row.append((r // a, g // a, b // a, a // (SS * SS)))
            else:
                row.append((0, 0, 0, 0))
        out.append(row)
    return out


def ico_image(rgba):
    """32bpp BMP + AND mask,符合 ICO 内嵌 DIB 格式。"""
    h = len(rgba)
    w = len(rgba[0])
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for (r, g, b, a) in rgba[y]:
            xor += bytes((b, g, r, a))
    mask_stride = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray(mask_stride)
        for x in range(w):
            if rgba[y][x][3] < 128:
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                         len(xor) + len(mask), 0, 0, 0, 0)
    return bytes(header + xor + mask)


def write_ico(path, sizes, dot):
    images = [(s, ico_image(render_rgba(s, dot))) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                           len(data), offset)
        offset += len(data)
    for _size, data in images:
        out += data
    with open(path, "wb") as f:
        f.write(out)


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, dot in STATES.items():
        path = os.path.join(OUT, "tray-%s.ico" % name)
        write_ico(path, (16, 20, 24, 32), dot)
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))
    # 默认/关于窗口用运行态
    write_ico(os.path.join(OUT, "app.ico"), (16, 24, 32, 48, 64),
              STATES["run"])


if __name__ == "__main__":
    main()
