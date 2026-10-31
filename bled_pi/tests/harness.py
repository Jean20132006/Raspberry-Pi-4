"""Test helpers: a fake DISPLAY that talks to the real controller through an in-memory serial cable."""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))   # make `import bled_unit` work from the tests folder

from bled_unit.config import Config
from bled_unit.controller import Controller
from bled_unit.fake_hardware import FakeHardware
from bled_unit.link import Link, MemoryPort
from bled_unit.protocol import Parser, frame


class FakeDisplay:
    """Plays the role of the ESP32 display: sends frames, records everything the unit sends back."""

    def __init__(self, port: MemoryPort) -> None:
        self.port = port
        self.parser = Parser()
        self.frames = []                                 # every message received, as (type, args)
        self._run = True
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        while self._run:
            data = self.port.read(64)
            if data:
                with self._lock:
                    for m in self.parser.feed(data):
                        self.frames.append((m.type, m.args))

    def send(self, body: str) -> None:
        self.port.write(frame(body))

    def send_raw(self, data: bytes) -> None:
        self.port.write(data)

    def types(self):
        with self._lock:
            return [f[0] for f in self.frames]

    def find(self, typ: str, pred=lambda a: True):
        with self._lock:
            return [f[1] for f in self.frames if f[0] == typ and pred(f[1])]

    def wait(self, typ: str, pred=lambda a: True, timeout: float = 6.0, after: int = 0):
        """Wait until a frame of this type (matching pred) arrives after position `after`. Returns args or None."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                for t, a in self.frames[after:]:
                    if t == typ and pred(a):
                        return a
            time.sleep(0.01)
        return None

    def mark(self) -> int:
        with self._lock:
            return len(self.frames)

    def stop(self) -> None:
        self._run = False


class Rig:
    """Everything wired together: fake hardware + controller thread + fake display."""

    def __init__(self, data_dir=None, **cfg_changes) -> None:
        self.tmp = None
        if data_dir is None:
            self.tmp = tempfile.TemporaryDirectory()
            data_dir = self.tmp.name
        from pathlib import Path
        self.cfg = dataclasses.replace(Config().fast(), data_dir=Path(data_dir), **cfg_changes)
        self.hw = FakeHardware(self.cfg)
        a, b = MemoryPort.pair()
        holder = {}
        self.link = Link(a, lambda m: holder["c"].on_message(m))
        self.ctrl = Controller(self.cfg, self.hw, self.link)
        holder["c"] = self.ctrl
        self.hw.set_start_callback(self.ctrl.on_start_button)
        self.display = FakeDisplay(b)
        self.link.start()
        self.thread = threading.Thread(target=self.ctrl.run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.ctrl.request_shutdown("test end")
        self.thread.join(timeout=5)
        self.link.stop()
        self.display.stop()
        if self.tmp:
            self.tmp.cleanup()

    def stat_state(self):
        """State code of the most recent STAT frame."""
        s = self.display.find("STAT")
        return int(s[-1][0]) if s else None

    def wait_state(self, code: int, timeout: float = 6.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.stat_state() == code:
                return True
            time.sleep(0.02)
        return False
