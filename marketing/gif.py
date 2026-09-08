# -*- coding: utf-8 -*-
"""Write an animated GIF, with no dependency to install.

There is no ffmpeg and no ImageMagick on this machine, and this project has
carried its own VAPID, its own SigV4, its own WebSocket and its own PDF path
rather than grow a dependency for one job. A GIF is the same size of problem:
decode the PNG frames the browser hands back, reduce them to 256 colours, and
write GIF89a with an LZW stream.

Only what a screen recording needs is here. No interlace, no transparency, no
local palettes: the frames all come from the same interface, so one palette
built from a sample of them fits the whole film.
"""
import zlib
from collections import defaultdict


# --------------------------------------------------------------------------- #
#  PNG in
# --------------------------------------------------------------------------- #
def png_rgb(data):
    """(width, height, bytearray of RGB triples) from a PNG the browser wrote."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w, h, depth, ctype = 8, [], 0, 0, 8, 6
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length                                   # 4 len, 4 kind, 4 crc
        if kind == b"IHDR":
            w = int.from_bytes(body[0:4], "big")
            h = int.from_bytes(body[4:8], "big")
            depth, ctype = body[8], body[9]
            if body[12] != 0:
                raise ValueError("interlaced PNG")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6):
        raise ValueError("expected 8-bit RGB or RGBA, got depth %d type %d" % (depth, ctype))

    chan = 3 if ctype == 2 else 4
    raw = zlib.decompress(b"".join(idat))
    stride = w * chan
    out = bytearray(h * w * 3)
    prev = bytearray(stride)
    at = 0
    for y in range(h):
        ftype = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        if ftype == 1:
            for i in range(chan, stride):
                line[i] = (line[i] + line[i - chan]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - chan] if i >= chan else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - chan] if i >= chan else 0
                b = prev[i]
                c = prev[i - chan] if i >= chan else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError("unknown PNG filter %d" % ftype)
        prev = line
        if chan == 3:
            out[y * w * 3:(y + 1) * w * 3] = line
        else:
            row = y * w * 3
            for x in range(w):
                s = x * 4
                out[row + x * 3:row + x * 3 + 3] = line[s:s + 3]
    return w, h, out


def box_shrink(w, h, rgb, factor):
    """Average factor x factor blocks. A browser shot at deviceScaleFactor 2
    shrunk this way is sharper than the same shot taken at 1."""
    if factor <= 1:
        return w, h, rgb
    nw, nh, n = w // factor, h // factor, factor * factor
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        base = y * factor
        for x in range(nw):
            r = g = b = 0
            for dy in range(factor):
                row = (base + dy) * w * 3 + x * factor * 3
                for dx in range(factor):
                    i = row + dx * 3
                    r += rgb[i]; g += rgb[i + 1]; b += rgb[i + 2]
            o = (y * nw + x) * 3
            out[o] = r // n; out[o + 1] = g // n; out[o + 2] = b // n
    return nw, nh, out


# --------------------------------------------------------------------------- #
#  256 colours
# --------------------------------------------------------------------------- #
def build_palette(frames, size=256):
    """Median cut over the colours actually present, weighted by how often.

    A fixed web palette bands the dark greys this interface is mostly made of,
    which is exactly where banding shows.
    """
    counts = defaultdict(int)
    for rgb in frames:
        for i in range(0, len(rgb), 3):
            counts[(rgb[i], rgb[i + 1], rgb[i + 2])] += 1
    colours = list(counts.items())
    if len(colours) <= size:
        pal = [c for c, _ in colours]
        return pal + [(0, 0, 0)] * (size - len(pal))

    boxes = [colours]
    while len(boxes) < size:
        boxes.sort(key=lambda b: -_spread(b))
        big = boxes.pop(0)
        if len(big) < 2:
            boxes.append(big)
            break
        axis = _widest_axis(big)
        big.sort(key=lambda cw: cw[0][axis])
        half = _weight_split(big)
        boxes += [big[:half], big[half:]]
    return [_mean(b) for b in boxes] + [(0, 0, 0)] * (size - len(boxes))


def _spread(box):
    if len(box) < 2:
        return 0
    lo = [255, 255, 255]; hi = [0, 0, 0]
    for (r, g, b), _ in box:
        lo[0] = min(lo[0], r); hi[0] = max(hi[0], r)
        lo[1] = min(lo[1], g); hi[1] = max(hi[1], g)
        lo[2] = min(lo[2], b); hi[2] = max(hi[2], b)
    return max(hi[i] - lo[i] for i in range(3))


def _widest_axis(box):
    lo = [255, 255, 255]; hi = [0, 0, 0]
    for c, _ in box:
        for i in range(3):
            lo[i] = min(lo[i], c[i]); hi[i] = max(hi[i], c[i])
    span = [hi[i] - lo[i] for i in range(3)]
    return span.index(max(span))


def _weight_split(box):
    total = sum(w for _, w in box)
    run = 0
    for i, (_, w) in enumerate(box):
        run += w
        if run * 2 >= total:
            return max(1, min(i + 1, len(box) - 1))
    return len(box) // 2


def _mean(box):
    tw = sum(w for _, w in box) or 1
    return tuple(sum(c[i] * w for c, w in box) // tw for i in range(3))


class Mapper(object):
    """Nearest palette entry, cached: a UI frame repeats its colours."""

    def __init__(self, palette):
        self.pal = palette
        self.cache = {}

    def index(self, rgb, i):
        key = (rgb[i], rgb[i + 1], rgb[i + 2])
        got = self.cache.get(key)
        if got is None:
            r, g, b = key
            best, bestd = 0, 1 << 30
            for n, (pr, pg, pb) in enumerate(self.pal):
                d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                if d < bestd:
                    best, bestd = n, d
                    if not d:
                        break
            got = self.cache[key] = best
        return got

    def frame(self, rgb):
        idx = self.index
        return bytes(idx(rgb, i) for i in range(0, len(rgb), 3))


# --------------------------------------------------------------------------- #
#  LZW out
# --------------------------------------------------------------------------- #
def _lzw(indices, min_code=8):
    clear, end = 1 << min_code, (1 << min_code) + 1
    table = {bytes([i]): i for i in range(clear)}
    nxt, width = end + 1, min_code + 1
    out, acc, nbits = bytearray(), 0, 0

    def emit(code):
        nonlocal acc, nbits
        acc |= code << nbits
        nbits += width
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8

    emit(clear)
    prefix = b""
    for byte in indices:
        cur = prefix + bytes([byte])
        if cur in table:
            prefix = cur
            continue
        emit(table[prefix])
        table[cur] = nxt
        nxt += 1
        if nxt > (1 << width) and width < 12:
            width += 1
        elif nxt > 4095:
            emit(clear)
            table = {bytes([i]): i for i in range(clear)}
            nxt, width = end + 1, min_code + 1
        prefix = bytes([byte])
    if prefix:
        emit(table[prefix])
    emit(end)
    if nbits:
        out.append(acc & 0xFF)

    blocks = bytearray()
    for i in range(0, len(out), 255):
        chunk = out[i:i + 255]
        blocks.append(len(chunk))
        blocks += chunk
    blocks.append(0)
    return bytes(blocks)


def write(path, frames, width, height, palette, delay_cs=25, loop=0):
    """frames: palette-index bytes, one per frame, width*height each."""
    g = bytearray()
    g += b"GIF89a"
    g += width.to_bytes(2, "little") + height.to_bytes(2, "little")
    g += bytes([0xF7, 0, 0])                                 # global table, 256
    for r, gg, b in palette:
        g += bytes([r, gg, b])
    g += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01" + loop.to_bytes(2, "little") + b"\x00"
    for data in frames:
        g += b"\x21\xF9\x04\x04" + delay_cs.to_bytes(2, "little") + b"\x00\x00"
        g += b"\x2C" + (0).to_bytes(2, "little") + (0).to_bytes(2, "little")
        g += width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x00"
        g += bytes([8]) + _lzw(data, 8)
    g += b"\x3B"
    with open(path, "wb") as f:
        f.write(bytes(g))
    return len(g)
