"""
protocol.py - the text messages exchanged with the display over the UART.

This is a Python version of the C file bled_proto.c running on the display (ESP32-S3), so both sides
speak exactly the same language.  A frame looks like this:

        $PAD,3,41.2*33E9\r\n
        |  |        |  |
        |  |        |  +-- 4 UPPER-case hex digits: CRC-16/CCITT-FALSE of the text between '$' and '*'
        |  |        +----- '*' separates the text from the checksum
        |  +-------------- message type, then comma separated arguments  (the "body")
        +----------------- '$' starts every frame

Why a checksum?  A wire can corrupt a byte.  If the checksum does not match, the frame is thrown away.
"""
from __future__ import annotations                       # modern type hints on older Python

import binascii                                          # binascii.crc_hqx computes CRC-16/CCITT
import re                                                # regular expressions to validate text
from typing import List, NamedTuple                      # types for readable code


# --------------------------------------------------------------------------- limits copied from bled_proto.h
MAX_BODY = 91                                            # longest allowed body in bytes
MAX_ARGS = 8                                             # most arguments allowed after the type
_TYPE_RE = re.compile(r"^[A-Z0-9]{1,7}$")                # message type: 1-7 characters A-Z or 0-9
_ARG_RE = re.compile(r"^[A-Za-z0-9.\-_+:]*$")            # one argument: only these characters are allowed


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: polynomial 0x1021, start value 0xFFFF, no reflection, no final XOR."""
    return binascii.crc_hqx(data, 0xFFFF)                # same algorithm as the C code (check: "123456789" -> 0x29B1)


def frame(body: str) -> bytes:
    """Turn a body such as 'PAD,3,41.2' into the bytes to send: b'$PAD,3,41.2*33E9\\r\\n'."""
    raw = body.encode("ascii")                           # text -> bytes (only plain ASCII is allowed)
    crc = "%04X" % crc16(raw)                            # checksum as exactly 4 upper-case hex digits
    return b"$" + raw + b"*" + crc.encode("ascii") + b"\r\n"   # assemble the complete frame


class Message(NamedTuple):
    """A received, checksum-verified message."""
    type: str                                            # e.g. "BASE", "CASE", "ENDC", "PING"
    args: List[str]                                      # the arguments as text, e.g. ["20260413", "13.2"]


class Parser:
    """Collects incoming bytes one at a time and returns complete, valid messages.

    Rules (same as the C parser): '$' always restarts a frame, '\\n' ends it, a wrong checksum or bad syntax
    drops the frame silently, so line noise between frames is harmless.
    """

    def __init__(self) -> None:
        self._buf: bytearray | None = None               # None = we are waiting for a '$'
        self.crc_errors = 0                              # how many frames were dropped (useful for debugging)

    def feed(self, data: bytes) -> List[Message]:
        """Give the parser new bytes; get back a (possibly empty) list of complete messages."""
        out: List[Message] = []                          # messages completed by this chunk of bytes
        for byte in data:                                # look at every received byte in order
            if byte == 0x24:                             # '$' = start of a new frame
                self._buf = bytearray()                  # start with an empty buffer (drops any half frame)
                continue                                 # go to the next byte
            if self._buf is None:                        # not inside a frame: ignore noise
                continue
            if byte == 0x0A:                             # '\n' = end of frame
                line = bytes(self._buf).rstrip(b"\r")    # remove the optional '\r' in front of '\n'
                self._buf = None                         # wait for the next '$'
                msg = self._decode(line)                 # check checksum and syntax
                if msg is not None:                      # valid?
                    out.append(msg)                      # keep it
                continue
            self._buf.append(byte)                       # a normal byte inside the frame: store it
            if len(self._buf) > MAX_BODY + 8:            # far too long -> this is garbage
                self._buf = None                         # give up on this frame
        return out                                       # hand back everything we completed

    def _decode(self, line: bytes) -> Message | None:
        """Validate 'BODY*CRC' and split BODY into type and arguments. Returns None if anything is wrong."""
        if len(line) < 6 or line[-5:-4] != b"*":         # need at least "T*XXXX" and a '*' before the 4 digits
            return None                                  # malformed
        body, crc = line[:-5], line[-4:]                 # split into body and the 4 checksum characters
        if len(body) > MAX_BODY:                         # body too long
            return None
        if crc != ("%04X" % crc16(body)).encode():       # compare with our own calculation (UPPER-case hex only)
            self.crc_errors += 1                         # count the failure
            return None                                  # corrupted frame -> drop it
        try:
            text = body.decode("ascii")                  # bytes -> text
        except UnicodeDecodeError:                       # non-ASCII byte inside the body
            return None
        parts = text.split(",")                          # "CASE,A1,13.2" -> ["CASE", "A1", "13.2"]
        if not _TYPE_RE.match(parts[0]):                 # message type must be 1-7 chars A-Z0-9
            return None
        if len(parts) - 1 > MAX_ARGS:                    # too many arguments
            return None
        if not all(_ARG_RE.match(a) for a in parts[1:]): # every argument must use only allowed characters
            return None
        return Message(parts[0], parts[1:])              # a good message


# --------------------------------------------------------------------------- builders for what WE send
def stat(state: int, cartridge: int, power: int, battery: int) -> str:
    """STAT,<state>,<cart>,<pwr>,<batt>   state: 0 idle, 1 baseline, 2 case active, 3 error."""
    return "STAT,%d,%d,%d,%d" % (state, cartridge, power, battery)   # all integers


def bprg(percent: int) -> str:
    """BPRG,<pct>  baseline progress 0-100."""
    return "BPRG,%d" % max(0, min(100, percent))         # clamp into 0..100 so we never send nonsense


def bdon(ok: bool, i540: int, i850: int) -> str:
    """BDON,<ok>,<i540>,<i850>  baseline finished; the two intensities are integers saved in the case file."""
    return "BDON,%d,%d,%d" % (1 if ok else 0, i540, i850)


def pad(n: int, ebl_ml: float) -> str:
    """PAD,<n>,<ebl_ml>  pad number n measured; volume of THIS pad in millilitres (one decimal)."""
    return "PAD,%d,%.1f" % (n, ebl_ml)                   # example: PAD,3,41.2


def err(code: int) -> str:
    """ERR,<code>  1 no cartridge, 2 sensor fault, 3 reagent empty, 4 optical over-range."""
    return "ERR,%d" % code
