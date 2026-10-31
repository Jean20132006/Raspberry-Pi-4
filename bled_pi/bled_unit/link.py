"""
link.py - sends and receives frames on the serial port (UART) connected to the display.

A background thread reads bytes, the Parser (protocol.py) turns them into messages, and each message is handed
to the controller through a callback.  Sending is done with send(), which is safe to call from any thread.

"Port" = any object with read(n) -> bytes, write(bytes) and close().  The real one is pyserial's Serial;
MemoryPort (below) is an in-memory pair used for tests and for the PC simulator.
"""
from __future__ import annotations

import logging                                           # messages for the log file / terminal
import queue                                             # thread-safe byte queue for MemoryPort
import threading
import time
from typing import Callable, Tuple

from .protocol import Message, Parser, frame

log = logging.getLogger("bled.link")                     # logger of this file


def open_serial(port_name: str, baud: int):             # open_serial(): opens the real serial port
    """Open the real serial port with pyserial.  Imported here so a PC without pyserial can still use MemoryPort."""
    import serial                                        # pip install pyserial
    # 8 data bits, no parity, 1 stop bit (8N1) is the default; timeout makes read() return after 0.1 s if idle
    return serial.Serial(port_name, baud, timeout=0.1)  # 8N1 is pyserial's default; timeout=0.1 makes read() return after 0.1 s of silence


class MemoryPort:                                       # a fake serial cable for tests and the PC simulator
    """One end of a fake serial cable.  Use MemoryPort.pair() to get two connected ends (A <-> B)."""

    def __init__(self, rx: "queue.Queue[int]", tx: "queue.Queue[int]") -> None:  # rx = bytes we receive, tx = bytes we send
        self._rx = rx                                    # bytes that arrive for us
        self._tx = tx                                    # bytes we send (they arrive at the other end)
        self.closed = False                             # set by close()

    @staticmethod                                       # static method: called as MemoryPort.pair(), no object needed
    def pair() -> Tuple["MemoryPort", "MemoryPort"]:    # pair(): create two connected ends
        """Create two ends connected crosswise, like TX <-> RX on a real cable."""
        a_to_b: "queue.Queue[int]" = queue.Queue()      # bytes travelling from end A to end B
        b_to_a: "queue.Queue[int]" = queue.Queue()      # bytes travelling from end B to end A
        return MemoryPort(b_to_a, a_to_b), MemoryPort(a_to_b, b_to_a)  # A's receive queue is B's send queue and vice versa (like crossed TX/RX)

    def read(self, size: int = 64) -> bytes:            # read(): like pyserial, returns up to `size` bytes or nothing after a timeout
        """Return up to `size` bytes; wait at most 0.05 s for the first one (like a serial timeout)."""
        out = bytearray()                               # buffer for the bytes we collect
        try:                                            # queue.Empty is raised when nothing is available
            out.append(self._rx.get(timeout=0.05))       # wait for the first byte
            while len(out) < size:                      # keep taking bytes that are already waiting
                out.append(self._rx.get_nowait())        # then take whatever else is already there
        except queue.Empty:                             # nothing (more) to read: fine
            pass                                         # nothing (more) to read
        return bytes(out)                               # return what we collected (may be empty)

    def write(self, data: bytes) -> int:                # write(): send bytes to the other end
        """Send bytes to the other end."""
        for b in data:                                  # go through the bytes one by one
            self._tx.put(b)                             # put each one into the other end's receive queue
        return len(data)                                # like pyserial: number of bytes written

    def close(self) -> None:                            # close(): nothing to release for a fake port
        self.closed = True                              # just remember that it is closed


class Link:                                             # the object the controller uses to talk frames
    """Frame-level communication with the display."""

    def __init__(self, port, on_message: Callable[[Message], None]) -> None:  # port = serial port, on_message = function called for each valid message
        self._port = port                                # serial port (or MemoryPort)
        self._on_message = on_message                    # called for every valid message received
        self._parser = Parser()                          # byte stream -> messages
        self._tx_lock = threading.Lock()                 # only one thread may write at a time
        self._running = False                            # reader thread flag
        self._thread: threading.Thread | None = None    # the receive thread (created in start())
        self.last_rx = 0.0                               # time of the last valid frame (for diagnostics)
        self.tx_count = 0                                # statistics
        self.rx_count = 0                               # number of frames received

    def start(self) -> None:                            # start(): begin receiving
        """Start the reader thread."""
        self._running = True                            # tell the thread to keep running
        self._thread = threading.Thread(target=self._reader, name="uart-rx", daemon=True)  # create the receive thread (daemon = ends with the program)
        self._thread.start()                            # start it

    def stop(self) -> None:                             # stop(): end receiving and close the port
        """Stop the reader thread and close the port."""
        self._running = False                           # tell the thread to finish
        if self._thread is not None:                    # wait for it (max 1 s)
            self._thread.join(timeout=1.0)               # wait until the thread has ended
        try:                                            # close the port, but never crash while doing it
            self._port.close()                          # release the serial device
        except Exception:                                # closing must never crash the shutdown
            pass                                        # ignore any error while closing

    def send(self, body: str) -> None:                  # send(): put one frame on the wire
        """Send one frame, e.g. send('STAT,0,1,0,87')."""
        data = frame(body)                               # add '$', checksum and line end
        with self._tx_lock:                              # no two threads may interleave their bytes
            try:                                        # writing can fail if the port is gone
                self._port.write(data)                   # put the bytes on the wire
                self.tx_count += 1                      # count the frame
            except Exception as exc:                     # port unplugged etc.: log, do not crash
                log.error("serial write failed: %s", exc)  # log the problem, keep running
        log.debug("TX %s", body)                        # debug log of every frame sent (use -v to see it)

    def _reader(self) -> None:                          # _reader(): thread body, runs until stop()
        """Thread body: read bytes forever, parse, deliver."""
        while self._running:                            # repeat until told to stop
            try:                                        # reading can fail if the adapter is unplugged
                data = self._port.read(64)               # blocks up to the port timeout
            except Exception as exc:                     # e.g. USB-serial adapter pulled out
                log.error("serial read failed: %s", exc)  # log the problem
                time.sleep(0.5)                          # do not spin the CPU
                continue                                # try again
            if not data:                                 # timeout, nothing received
                continue                                # no bytes: loop again
            for msg in self._parser.feed(data):          # complete, valid messages
                self.last_rx = time.monotonic()         # remember when the last valid frame arrived
                self.rx_count += 1                      # count the frame
                log.debug("RX %s %s", msg.type, msg.args)  # debug log of every frame received
                try:                                    # protect the thread from bugs in the handler
                    self._on_message(msg)                # hand over to the controller
                except Exception:                        # a bug in the handler must not kill the reader
                    log.exception("message handler failed")  # log the bug but keep receiving
