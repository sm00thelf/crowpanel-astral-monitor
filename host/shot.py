#!/usr/bin/env python3
"""Save what the CrowPanel shows right now as a PNG. Takes about 25 s; the feed may keep running.

  ./shot.py [out.png]
  ./shot.py --page 4 [out.png]    switch the panel to that page first
  ./shot.py --selftest
"""

import argparse
import os
import re
import select
import struct
import sys
import time
import zlib

import feed


def rgb(raw, flipped):
    """RGB565 little-endian framebuffer -> RGB888 bytes; a flipped panel is rotated back."""
    px = struct.unpack("<%dH" % (len(raw) // 2), raw)
    if flipped:
        px = px[::-1]              # 180 degrees = pixel order reversed
    out = bytearray()
    for v in px:
        r, g, b = v >> 11, v >> 5 & 63, v & 31
        out += bytes((r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2))
    return bytes(out)


def png(w, h, data):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = b"".join(b"\0" + data[y * w * 3:(y + 1) * w * 3] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def read(fd, n, timeout):
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        sys.exit("no answer from the panel: is the firmware older than the screenshot command?")
    return os.read(fd, n)


def selftest():
    white_red = struct.pack("<2H", 0xFFFF, 0xF800)
    assert rgb(white_red, 0) == b"\xff\xff\xff\xff\x00\x00"
    assert rgb(white_red, 1) == b"\xff\x00\x00\xff\xff\xff"
    assert png(2, 1, rgb(white_red, 0))[:8] == b"\x89PNG\r\n\x1a\n"
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", default="screenshot.png")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--page", type=int, help="switch the panel to this page first, counted from 1")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    try:
        fd = feed.open_port(a.port, os.O_RDWR)
    except OSError as e:
        sys.exit("%s: %s (in the dialout group, and logged in again since?)" % (a.port, e.strerror))
    if a.page:
        os.write(fd, b"\npage %d\n" % a.page)
        time.sleep(1.5)            # until the next frame of the feed has been drawn on it
    os.write(fd, b"\nshot\n")
    buf, m = b"", None
    while not m:                   # skip log output until the header line
        buf += read(fd, 4096, 5)
        m = re.search(rb"SHOT (\d+) (\d+) ([01])\n", buf)
    w, h, flipped = map(int, m.groups())
    raw = buf[m.end():]
    while len(raw) < w * h * 2:
        raw += read(fd, w * h * 2 - len(raw), 5)
    with open(a.out, "wb") as f:
        f.write(png(w, h, rgb(raw, flipped)))
    print("%s: %dx%d" % (a.out, w, h))


if __name__ == "__main__":
    main()
