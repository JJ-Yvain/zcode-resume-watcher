# -*- coding: utf-8 -*-
"""生成应用/托盘图标与面板 favicon(纯标准库,无 Pillow)。

品牌标来自 `assets/brand-mark.png`(白色 + alpha 母版,由品牌稿提取);
图标 = 深炭圆盘 + 暖白细环 + 状态色标,16px 下仍可辨认,亮/暗任务栏都可见。
    python desktop/make_icons.py
"""
import math
import os
import struct
import zlib

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
MASK_FILE = os.path.join(OUT, "brand-mark.png")


def read_png(path):
    """解码 8-bit 非隔行 PNG,返回 (width, height, channels, rows)。"""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)
    pos, idat, w, h, ct = 8, b"", 0, 0, 0
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            w, h, depth, ct, _comp, _filt, inter = struct.unpack(
                ">IIBBBBB", chunk)
            if depth != 8 or inter != 0:
                raise ValueError("unsupported PNG: %s" % path)
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + length
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    stride = w * channels
    raw = zlib.decompress(idat)
    rows, prev, i = [], bytearray(stride), 0
    for _y in range(h):
        filt = raw[i]
        i += 1
        line = bytearray(raw[i:i + stride])
        i += stride
        if filt == 1:
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 255
        elif filt == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif filt == 3:
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif filt == 4:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, channels, rows


def write_png_rgba(path, rgba):
    with open(path, "wb") as f:
        f.write(png_bytes(rgba))


def png_bytes(rgba):
    """把 RGBA 行列表编码成 8-bit RGBA PNG(filter 0)。"""
    h = len(rgba)
    w = len(rgba[0])
    raw = bytearray()
    for row in rgba:
        raw.append(0)
        for (r, g, b, a) in row:
            raw += bytes((r, g, b, a))

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += chunk(b"IEND", b"")
    return out


def write_icns(path, sizes, mark, mask):
    """写 macOS .icns；PNG 类型 icp4/5/6、ic07/08/09。"""
    kinds = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07",
             256: b"ic08", 512: b"ic09"}
    chunks = []
    for size in sizes:
        kind = kinds.get(size)
        if kind is None:
            continue
        # 大尺寸降超采样，纯 Python 下控制生成时间。
        ss = 2 if size >= 256 else (3 if size >= 128 else 4)
        data = png_bytes(render_rgba(size, mark, mask, ss=ss))
        chunks.append(kind + struct.pack(">I", 8 + len(data)) + data)
    body = b"".join(chunks)
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def load_mask(path=MASK_FILE):
    """加载 brand-mark.png,返回 (w, h, alpha_rows)。"""
    w, h, channels, rows = read_png(path)
    if channels not in (2, 4):
        raise ValueError("brand mark needs an alpha channel: %s" % path)
    alpha = [[row[x * channels + channels - 1] for x in range(w)]
             for row in rows]
    return w, h, alpha


def _mask_alpha(mask, u, v):
    """双线性采样覆盖度,u/v ∈ [0,1]。"""
    w, h, alpha = mask
    x = min(max(u, 0.0), 1.0) * (w - 1)
    y = min(max(v, 0.0), 1.0) * (h - 1)
    x0, y0 = int(x), int(y)
    x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
    fx, fy = x - x0, y - y0
    a = alpha[y0][x0]
    b = alpha[y0][x1]
    c = alpha[y1][x0]
    d = alpha[y1][x1]
    return (a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) +
            c * (1 - fx) * fy + d * fx * fy)


def _blend(base, top, alpha):
    return tuple(int(base[i] + (top[i] - base[i]) * alpha / 255.0 + 0.5)
                 for i in range(3))


def render_rgba(size, mark, mask, ss=None):
    """返回 size×size 的 RGBA 行列表(自上而下)。

    深炭圆盘 + 暖白细环 + 品牌标(状态色/暖白);圆盘外透明。
    """
    ss = ss or SS
    n = size * ss
    cx = cy = (n - 1) / 2.0
    r_out = 0.46 * n
    rim = 0.042 * n
    mask_w, mask_h = mask[0], mask[1]
    mark_w = 0.68 * n
    mark_h = mark_w * mask_h / mask_w
    buf = []
    for py in range(n):
        row = []
        for px in range(n):
            d = math.hypot(px - cx, py - cy)
            if d > r_out:
                row.append((0, 0, 0, 0))
                continue
            rgb = CREAM if d > r_out - rim else INK
            u = (px - cx) / mark_w + 0.5
            v = (py - cy) / mark_h + 0.5
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                a = _mask_alpha(mask, u, v)
                if a > 0:
                    rgb = _blend(rgb, mark, a)
            row.append((rgb[0], rgb[1], rgb[2], 255))
        buf.append(row)
    # 盒式降采样
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            r = g = b = a = 0
            for yy in range(ss):
                for xx in range(ss):
                    p = buf[y * ss + yy][x * ss + xx]
                    r += p[0] * p[3]
                    g += p[1] * p[3]
                    b += p[2] * p[3]
                    a += p[3]
            if a:
                row.append((r // a, g // a, b // a, a // (ss * ss)))
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


def write_ico(path, sizes, mark, mask):
    images = [(s, ico_image(render_rgba(s, mark, mask))) for s in sizes]
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
    mask = load_mask()
    for name, mark in STATES.items():
        path = os.path.join(OUT, "tray-%s.ico" % name)
        write_ico(path, (16, 20, 24, 32), mark, mask)
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))
    # 默认/关于窗口用运行态;面板 favicon 用同一张 64px 渲染
    app = os.path.join(OUT, "app.ico")
    write_ico(app, (16, 24, 32, 48, 64, 128, 256), STATES["run"], mask)
    print("wrote %s (%d bytes)" % (app, os.path.getsize(app)))
    icns = os.path.join(OUT, "app.icns")
    write_icns(icns, (16, 32, 64, 128, 256, 512), STATES["run"], mask)
    print("wrote %s (%d bytes)" % (icns, os.path.getsize(icns)))
    favicon = os.path.join(OUT, "brand-favicon.png")
    write_png_rgba(favicon, render_rgba(64, STATES["run"], mask))
    print("wrote %s (%d bytes)" % (favicon, os.path.getsize(favicon)))


if __name__ == "__main__":
    main()
