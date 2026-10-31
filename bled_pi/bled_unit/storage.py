"""
storage.py - everything that is written to the Raspberry Pi's own memory card.

Two things are stored:
  * CaseLogger      one CSV file per case, append-only, every row forced to disk (fsync) so a power cut
                    can lose at most the row that was being written.
  * ActiveCaseStore a tiny JSON file that remembers which case is open and how many pads were measured.
                    After a reboot in the middle of a case the pad counter continues instead of restarting
                    at 1 (the display ignores pad numbers that are not larger than the last one it accepted).
"""
from __future__ import annotations

import csv                                               # write the case file as CSV
import json                                              # write the active-case file as JSON
import os                                                # fsync, replace
import re                                                # validate case ids (also protects the file name)
from datetime import datetime, timezone                  # timestamps in UTC
from pathlib import Path
from typing import Optional

CASE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,12}$")        # same rule as the display: 1-12 letters, digits, - or _
HB_MIN, HB_MAX = 4.0, 20.0                               # accepted patient haemoglobin range in g/dL (same as the display)

HEADER = ["utc_time", "case_id", "event", "pad_n", "hb_patient",  # column names of every case CSV file
          "dark", "blank540", "blank850", "sample540", "sample850",  # (continued)
          "a540", "a850", "net_a", "ebl_ml", "note"]     # column names of every case file


def _now() -> str:                                      # _now(): timestamp helper
    """Current time as an ISO string in UTC, e.g. 2026-10-26T22:39:01Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # format like 2026-10-26T22:39:01Z


def valid_case(case_id: str, hb_text: str) -> Optional[float]:  # valid_case(): checks the id and Hb from the CASE message
    """Return the haemoglobin as a float if the id and Hb are acceptable, else None."""
    if not CASE_ID_RE.match(case_id):                    # bad characters or length
        return None                                     # bad id
    try:                                                # Hb must be a number
        hb = float(hb_text)                              # "13.2" -> 13.2
    except ValueError:                                   # not a number
        return None                                     # not a number
    if not (HB_MIN <= hb <= HB_MAX):                     # outside the plausible range
        return None                                     # outside 4-20 g/dL
    return hb                                           # all good: return the number


class CaseLogger:                                       # one CSV file per case
    """Append-only CSV log of one case."""

    def __init__(self, folder: Path, case_id: str, hb: float) -> None:  # open (or create) the file
        folder.mkdir(parents=True, exist_ok=True)        # create the folder the first time
        self.path = folder / ("%s.csv" % case_id)        # e.g. ~/bled_data/cases/A1234.csv
        self.case_id = case_id                          # remember the case id
        self.hb = hb                                    # remember the patient Hb
        is_new = not self.path.exists()                  # write the header only into a brand new file
        self._fh = open(self.path, "a", newline="", encoding="utf-8")   # "a" = append, never overwrite old rows
        self._csv = csv.writer(self._fh)                 # CSV writer on top of the file
        if is_new:                                      # only a brand-new file gets the column names
            self._row(HEADER)                            # first line = column names

    def _row(self, values: list) -> None:               # _row(): write one line and force it to the card
        """Write one row and force it onto the memory card."""
        self._csv.writerow(values)                       # put the row into Python's buffer
        self._fh.flush()                                 # push the buffer to the operating system
        os.fsync(self._fh.fileno())                      # ask the OS to write it to the card NOW (survives a power cut)

    def event(self, name: str, note: str = "") -> None:  # event(): a line without measurement numbers
        """Log an event without measurement numbers (START, BASELINE, ERROR, END ...)."""
        self._row([_now(), self.case_id, name, "", "%.1f" % self.hb] + [""] * 9 + [note])  # empty columns for the numbers, text in the last column

    def baseline(self, ok: bool, i540: int, i850: int, note: str = "") -> None:  # baseline(): log the baseline result
        """Log the baseline result."""
        self._row([_now(), self.case_id, "BASELINE_OK" if ok else "BASELINE_FAIL", "", "%.1f" % self.hb,  # one row with the two baseline intensities
                   "", i540, i850, "", "", "", "", "", "", note])  # (rest of the row, empty columns)

    def pad(self, n: int, dark: float, blank540: float, blank850: float, sample540: float, sample850: float,  # pad(): log one measured pad
            a540: float, a850: float, net_a: float, ebl_ml: float, flags: list) -> None:  # (parameters continue)
        """Log one measured pad with all raw numbers, so the result can be re-calculated later."""
        self._row([_now(), self.case_id, "PAD", n, "%.1f" % self.hb,  # time, case, PAD, pad number, Hb,
                   "%.1f" % dark, "%.1f" % blank540, "%.1f" % blank850, "%.1f" % sample540, "%.1f" % sample850,  # dark level, blanks, samples,
                   "%.4f" % a540, "%.4f" % a850, "%.4f" % net_a, "%.2f" % ebl_ml, " ".join(flags)])  # absorbances, net absorbance, millilitres, warning flags

    def close(self) -> None:                            # close(): end of the case file
        """Close the file."""
        try:                                            # closing may fail if the card was removed
            self._fh.close()                            # write remaining data and close
        except OSError:                                 # card problem
            pass                                        # nothing else we can do


class ActiveCaseStore:                                  # remembers the open case across reboots
    """Remembers the open case across a reboot (JSON file, replaced atomically)."""

    def __init__(self, folder: Path) -> None:           # folder = where the JSON file lives
        folder.mkdir(parents=True, exist_ok=True)       # create the folder if it does not exist
        self.path = folder / "active_case.json"          # the state file

    def save(self, case_id: str, hb: float, pad_n: int) -> None:  # save(): write the state safely (see docstring)
        """Write the state to a temporary file, flush it to disk, then rename it over the real file.

        The rename is atomic: after a power cut the file is either the complete OLD version or the complete
        NEW version, never half written.
        """
        tmp = self.path.with_suffix(".tmp")              # temporary neighbour file
        with open(tmp, "w", encoding="utf-8") as fh:     # create/overwrite the temporary file
            json.dump({"case_id": case_id, "hb": hb, "pad_n": pad_n}, fh)   # the state
            fh.flush()                                   # to the OS ...
            os.fsync(fh.fileno())                        # ... and to the card
        os.replace(tmp, self.path)                       # atomic swap

    def load(self) -> Optional[dict]:                   # load(): read the state, or None
        """Return the saved state, or None when there is none (or it is unreadable)."""
        try:                                            # reading may fail
            with open(self.path, "r", encoding="utf-8") as fh:  # open the JSON file
                data = json.load(fh)                     # read the JSON
            if CASE_ID_RE.match(str(data["case_id"])) and int(data["pad_n"]) >= 0:   # sanity check
                return {"case_id": str(data["case_id"]), "hb": float(data["hb"]), "pad_n": int(data["pad_n"])}  # a complete, plausible state
        except (OSError, ValueError, KeyError, TypeError):   # missing file or damaged content
            pass                                        # missing or damaged file: act as if there is none
        return None                                     # no saved case

    def clear(self) -> None:                            # clear(): forget the open case
        """Forget the open case (called when the case ends)."""
        try:                                            # the file may not exist
            self.path.unlink()                           # delete the file
        except FileNotFoundError:                        # nothing to delete
            pass                                        # nothing to delete: fine
