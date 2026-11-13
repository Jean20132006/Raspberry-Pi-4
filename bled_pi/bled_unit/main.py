"""
main.py - starts the program.

    On the Raspberry Pi:    python3 -m bled_unit.main

Start-up order: logging -> hardware -> serial link -> controller -> run until Ctrl-C / SIGTERM / power fail.
"""
from __future__ import annotations

import argparse                                          # command line options
import logging
import logging.handlers                                  # rotating log file
import os                                                # systemd watchdog socket
import signal                                            # Ctrl-C and "systemctl stop"
import socket                                            # talk to systemd
import sys
import threading
import time

from .config import Config
from .controller import Controller
from .link import Link, open_serial


def setup_logging(cfg: Config, verbose: bool) -> None:  # setup_logging(): where messages go
    """Log to the terminal (journald on the Pi) and to a rotating file in the data folder."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)      # make sure the folder exists
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-10s %(message)s")   # one line per message
    root = logging.getLogger()                           # the top-level logger
    root.setLevel(logging.DEBUG if verbose else logging.INFO)   # -v shows every frame sent/received
    console = logging.StreamHandler()                    # terminal
    console.setFormatter(fmt)                           # same format on the terminal ...
    root.addHandler(console)                            # ... add the terminal handler
    # keep 5 files of 1 MB each so the log can never fill the memory card
    fileh = logging.handlers.RotatingFileHandler(cfg.data_dir / "bled_unit.log", maxBytes=1_000_000, backupCount=5)  # a log FILE that never grows beyond 6 MB (5 backups of 1 MB)
    fileh.setFormatter(fmt)                             # same format in the file
    root.addHandler(fileh)                              # add the file handler


def systemd_watchdog(controller: Controller, interval_s: float = 5.0) -> None:  # systemd_watchdog(): optional, only active when started as a service
    """If the service file has WatchdogSec=, systemd restarts us when we stop sending "I am alive".

    We only report alive while the controller thread still processes events, so a frozen controller gets restarted.
    """
    addr = os.environ.get("NOTIFY_SOCKET")               # set by systemd; absent when started by hand
    if not addr:                                        # not started by systemd: nothing to do
        return                                           # nothing to do
    if addr.startswith("@"):                             # abstract socket names start with '@'
        addr = "\0" + addr[1:]                          # abstract socket names start with '@' and need a leading zero byte
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)  # a datagram socket to systemd
    sock.sendto(b"READY=1", addr)                        # "start-up finished"

    def heartbeat() -> None:                            # heartbeat thread body
        while True:                                     # repeat forever
            time.sleep(interval_s)                      # wait 5 s between heartbeats
            if controller.clock() - controller.last_poll < 3.0:   # only while the controller loop is really running
                sock.sendto(b"WATCHDOG=1", addr)         # "still alive"
    threading.Thread(target=heartbeat, name="watchdog", daemon=True).start()  # start the heartbeat thread


def main(argv=None) -> int:                             # main(): the program entry point
    ap = argparse.ArgumentParser(description="BLED measurement unit (Raspberry Pi 4)")  # describes the command line options
    ap.add_argument("--port", help="serial port (default from config.py: /dev/serial0)")  # --port overrides the serial port from config.py
    ap.add_argument("-v", "--verbose", action="store_true", help="log every frame")  # -v = log every frame
    args = ap.parse_args(argv)                          # read the command line

    cfg = Config()                                       # default settings (edit config.py to change them)
    if args.port:                                       # a port was given on the command line
        cfg.serial_port = args.port                      # command line overrides config.py
    setup_logging(cfg, args.verbose)                    # start logging
    log = logging.getLogger("bled.main")                # logger for this file

    from .hardware import PiHardware
    hw = PiHardware(cfg)                                # the real Raspberry Pi hardware

    port = open_serial(cfg.serial_port, cfg.baud)        # open /dev/serial0
    holder: dict = {}                                    # lets the Link callback find the controller created afterwards
    link = Link(port, lambda m: holder["c"].on_message(m))  # the Link forwards each message to the controller (created next)
    ctrl = Controller(cfg, hw, link)                    # the controller owns all logic
    holder["c"] = ctrl                                  # now the Link callback can find it
    hw.set_start_callback(ctrl.on_start_button)          # START button -> event

    def stop(signum, _frame) -> None:                    # Ctrl-C or "systemctl stop"
        ctrl.request_shutdown("signal %d" % signum)     # ask the controller to stop cleanly
    signal.signal(signal.SIGINT, stop)                  # Ctrl-C
    signal.signal(signal.SIGTERM, stop)                 # systemctl stop

    link.start()                                         # start receiving
    systemd_watchdog(ctrl)                               # tell systemd we are alive (only when run as a service)
    log.info("BLED measurement unit running on %s", cfg.serial_port)  # tell the user we are running
    try:                                                # always clean up, even after an error
        ctrl.run()                                       # blocks until shutdown
    finally:                                            # close the serial port
        link.stop()                                      # close the serial port
        hw.close()                                       # LEDs and motor off, release the pins
    log.info("stopped")                                 # last log line
    return 0                                            # 0 = success


if __name__ == "__main__":                              # run main() only when the file is started directly
    sys.exit(main())                                    # give main()'s result to the operating system
