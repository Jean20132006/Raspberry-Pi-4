#!/usr/bin/env python3
"""
fake_display.py - pretends to be the DISPLAY, so you can test the Raspberry Pi software without the display.

Connect a 3.3 V USB-serial adapter to the Raspberry Pi UART (crossed) and run this on your PC:

    pip install pyserial
    python3 tools/fake_display.py COM5            (Windows)      python3 tools/fake_display.py /dev/ttyUSB0

or, on the Pi itself, with two USB-serial adapters wired to each other, or with --port pointing to a pty pair.

It sends PING every second (like the real display) and decodes everything the Pi sends.  Type + Enter:
    b                 send BASE (run the baseline)
    c <id> <hb>       send CASE, e.g.  c A1234 13.2
    e                 send ENDC
    q                 quit
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # find the bled_unit package
from bled_unit.protocol import Parser, frame               # same frame code as the unit


def run(port, lines, out=print, ping_period=1.0):
    """Talk over `port` (anything with read/write). `lines` yields the commands typed by the user."""
    parser, running = Parser(), [True]

    def reader():
        while running[0]:
            data = port.read(64)                            # bytes from the Pi
            for m in parser.feed(data):
                out("<- %-5s %s" % (m.type, ",".join(m.args)))   # print decoded message

    def pinger():
        while running[0]:
            port.write(frame("PING"))                       # heartbeat, as the real display does
            time.sleep(ping_period)

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=pinger, daemon=True).start()
    for line in lines:
        w = line.split()
        if not w:
            continue
        if w[0] == "b":
            port.write(frame("BASE")); out("-> BASE")
        elif w[0] == "c" and len(w) == 3:
            port.write(frame("CASE,%s,%s" % (w[1], w[2]))); out("-> CASE %s %s" % (w[1], w[2]))
        elif w[0] == "e":
            port.write(frame("ENDC")); out("-> ENDC")
        elif w[0] == "q":
            break
        else:
            out("commands: b | c <id> <hb> | e | q")
    running[0] = False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    import serial                                           # pip install pyserial
    p = serial.Serial(sys.argv[1], 115200, timeout=0.1)
    run(p, sys.stdin)
