"""
controller.py - the brain: ONE state machine that owns all decisions.

Everything that happens is turned into an EVENT and put in one queue (slide "one controller, many producers"):

    UART thread        -> MSG       a valid frame arrived from the display (BASE, CASE, ENDC, PING)
    START button       -> BUTTON    the operator pressed START
    ticker thread      -> POLL      every 0.25 s: check cartridge / power, send STAT, resend frames
    worker thread      -> JOB_*     a baseline or pad job reported progress / finished / failed

The controller takes ONE event at a time, so nothing can change the state behind its back - no race conditions.

States (STAT code sent to the display):
    BOOT, IDLE (0), BASELINE (1), READY (0), CASE_ACTIVE (2), ERROR (3), SHUTDOWN
"""
from __future__ import annotations

import logging
import queue
import subprocess                                        # optional OS shutdown after a power-fail warning
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional

from . import protocol as proto                          # frame builders: stat(), pad(), err() ...
from .config import Config
from .hardware import Hardware
from .jobs import BaselineResult, JobError, PadContext, PadOutcome, run_baseline, run_pad
from .link import Link
from .sensor import Cancelled, Reading
from .storage import ActiveCaseStore, CaseLogger, valid_case

log = logging.getLogger("bled.ctrl")                    # logger named 'bled.ctrl' so log lines show where they come from


class State(Enum):                                      # the list of possible states
    """The states of the measurement unit."""
    BOOT = "BOOT"                                        # power-on self-test
    IDLE = "IDLE"                                        # waiting for BASE (or for a CASE to resume)
    BASELINE = "BASELINE"                                # baseline measurement running
    READY = "READY"                                      # baseline valid, waiting for CASE
    CASE_ACTIVE = "CASE_ACTIVE"                          # a case is open; START measures a pad
    ERROR = "ERROR"                                      # a fault; waits for the operator
    SHUTDOWN = "SHUTDOWN"                                # leaving the program


STAT_CODE = {                                            # the number the display understands
    State.BOOT: 0, State.IDLE: 0, State.READY: 0,       # BOOT, IDLE and READY all look like 'idle' (0) to the display
    State.BASELINE: 1, State.CASE_ACTIVE: 2, State.ERROR: 3, State.SHUTDOWN: 0,  # BASELINE = 1, CASE_ACTIVE = 2, ERROR = 3, SHUTDOWN = 0
}                                                       # end of the STAT code table


@dataclass                                              # a dataclass automatically gets __init__ from its fields
class Event:                                            # one entry of the event queue
    """One item in the event queue."""
    kind: str                                            # "MSG", "BUTTON", "POLL", "JOB_PROGRESS", "JOB_DONE", "JOB_FAIL", "SHUTDOWN"
    data: Any = None                                     # payload (depends on the kind)


class Controller:                                       # the controller class
    """The state machine plus everything it owns (current case, current job, resend list)."""

    def __init__(self, cfg: Config, hw: Hardware, link: Link, clock: Callable[[], float] = time.monotonic) -> None:  # constructor: receives everything the controller needs
        self.cfg, self.hw, self.link = cfg, hw, link     # settings, hardware, serial link
        self.clock = clock                               # time source (replaced by a fake clock in tests)
        self.q: "queue.Queue[Event]" = queue.Queue()     # THE event queue

        self.state = State.BOOT                          # current state
        self.return_state = State.IDLE                   # where to go back to after an error is cleared (history state "H")
        self.error_code = 0                              # last ERR code (0 = none)

        self.baseline_ref: Optional[Reading] = None      # reference light levels from the last good baseline
        self.baseline_done_at = -1e9                     # time the last baseline ended (to ignore stale BASE repeats)
        self.pending_baseline: Optional[tuple] = None    # (ok, i540, i850) to write into the case file when it opens

        self.case_id: Optional[str] = None               # id of the open case (None = no case)
        self.hb = 0.0                                    # patient baseline haemoglobin of the open case
        self.pad_n = 0                                   # number of pads measured so far in this case
        self.total_ml = 0.0                              # running total, only for the log
        self.logger: Optional[CaseLogger] = None         # CSV file of the open case
        self.store = ActiveCaseStore(cfg.data_dir)       # remembers the open case across reboots
        self.pad_ctx = PadContext()                      # progress of the pad being measured (for resume after an error)

        self.job_thread: Optional[threading.Thread] = None   # the worker thread (or None)
        self.job_cancel = threading.Event()              # set() = tell the worker to stop
        self.job_id = 0                                  # numbers jobs so stale results can be recognised
        self.job_kind = ""                               # "baseline" or "pad" while a job runs
        self._last_pct = -1                              # last BPRG percentage sent

        self._last_stat = -1e9                           # when STAT was last sent
        self._last_err = -1e9                            # when ERR was last sent
        self._last_cart: Optional[bool] = None           # previous cartridge state (to detect changes)
        self._resend: List[list] = []                    # frames to repeat: [body, repeats_left, next_time]
        self._ticker_running = False                    # True while the ticker thread should keep running
        self.last_poll = clock()                         # last time the loop handled a POLL (used by the systemd watchdog)

    # ================================================================================== public helpers
    def post(self, kind: str, data: Any = None) -> None:  # post(): add an event to the queue
        """Put an event in the queue (safe from any thread)."""
        self.q.put(Event(kind, data))                   # queue.Queue is thread-safe, so any thread may call this

    def on_message(self, msg) -> None:                  # called by the UART thread for every valid frame
        """Callback for the UART thread: wrap the message into an event."""
        self.post("MSG", msg)                           # turn the frame into an event for the main loop

    def on_start_button(self) -> None:                  # called by the hardware layer when START is pressed
        """Callback for the START button (gpiozero thread): wrap into an event."""
        self.post("BUTTON")                             # turn the button press into an event

    def request_shutdown(self, reason: str = "requested") -> None:  # called by Ctrl-C / SIGTERM handlers
        """Ask the main loop to finish (used by Ctrl-C / SIGTERM)."""
        self.post("SHUTDOWN", reason)                   # turn the request into an event

    # ================================================================================== main loop
    def run(self) -> None:                              # run(): the main loop, never returns until shutdown
        """Run until SHUTDOWN.  This is the heart of the program: take one event, handle it, repeat."""
        self._boot()                                     # self-test, restore info
        self._ticker_running = True                     # tell the ticker thread it may run
        threading.Thread(target=self._ticker, name="ticker", daemon=True).start()   # POLL events
        handlers = {                                     # event kind -> method that handles it
            "MSG": self._on_msg, "BUTTON": self._on_button, "POLL": self._on_poll,  # message from the display, START button, 0.25 s poll
            "JOB_PROGRESS": self._on_job_progress, "JOB_DONE": self._on_job_done,  # progress / success / failure reports from worker threads
            "JOB_FAIL": self._on_job_fail, "SHUTDOWN": lambda reason: self._shutdown(str(reason)),  # SHUTDOWN: reason text is passed to _shutdown()
        }                                               # end of the handler table
        while self.state is not State.SHUTDOWN:          # loop until we are told to stop
            try:                                        # protect against an empty queue
                ev = self.q.get(timeout=0.5)             # wait for the next event
            except queue.Empty:                         # no event arrived within 0.5 s
                continue                                 # nothing happened: look again
            handler = handlers.get(ev.kind)              # find the handler
            if handler is None:                          # unknown event kind = programming error
                log.error("unknown event %s", ev.kind)  # should never happen: log it
                continue                                # skip this event and read the next one
            try:                                        # run the handler inside a safety net
                handler(ev.data)                         # do the work
            except Exception:                            # a bug must NOT stop the machine: log it and go to ERROR
                log.exception("error while handling %s", ev.kind)  # write the full traceback to the log
                self._enter_error(2, "internal error", self.state if self.state is not State.ERROR else self.return_state)  # stop motor/LEDs and tell the display; remember where to come back to
        self._ticker_running = False                    # the loop ended: stop the ticker thread
        self._cleanup()                                  # leave outputs off, close files

    def _ticker(self) -> None:                          # ticker thread body
        """Thread: post a POLL event at a fixed rate."""
        while self._ticker_running:                     # repeat until the controller stops
            self.post("POLL")                           # ask the controller to do its periodic checks
            time.sleep(self.cfg.poll_period_s)          # wait 0.25 s (poll_period_s) before the next POLL

    # ================================================================================== start / stop
    def _boot(self) -> None:                            # _boot(): power-on sequence
        """Power-on: switch everything off, test the ADC, look for an unfinished case."""
        self.state = State.BOOT                         # we are booting
        self.hw.leds_off()                               # known safe state
        self.hw.motor_stop()                            # motor off as well
        if not self.cfg.calibration_confirmed:           # shout so nobody mistakes the numbers for calibrated ones
            log.warning("CALIBRATION IS NOT CONFIRMED - results use placeholder constants (see config.py)")  # warn in the log every time the program starts
        saved = self.store.load()                        # was a case open when we last lost power?
        if saved:                                       # an unfinished case exists on the memory card
            log.warning("unfinished case %s found (%d pads). It resumes when the display sends CASE again.",  # tell the engineer what was found
                        saved["case_id"], saved["pad_n"])  # case id and number of pads already measured
        if self.hw.self_test():                          # can we talk to the ADC?
            self.state = State.IDLE                     # ADC is fine: ready for work
            log.info("boot OK -> IDLE")                 # log the result
        else:                                           # ADC does not answer
            log.error("ADC self-test failed")           # log the problem
            self._enter_error(2, "ADC self-test failed", State.IDLE)  # go to ERROR; after the fault clears return to IDLE

    def _shutdown(self, reason: str) -> None:           # _shutdown(): stop safely
        """Stop everything safely.  The open case stays saved so it can resume after the next power-up."""
        log.warning("shutting down: %s", reason)        # say why we stop
        self._cancel_job()                               # stop motor and LEDs
        if self.logger:                                 # is a case file open?
            self.logger.event("SHUTDOWN", reason)        # note it in the case file
        self.state = State.SHUTDOWN                      # makes run() leave its loop
        if self.cfg.power_fail_shutdown_os and "power" in reason:  # optional: also power down the Raspberry Pi itself
            try:                                        # run the OS command safely
                subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)   # halt the Raspberry Pi cleanly
            except OSError:                             # sudo missing or command not found
                log.error("could not run shutdown")     # log it, nothing else we can do

    def _cleanup(self) -> None:                         # _cleanup(): last actions of the program
        """Final tidy-up after the loop ended."""
        self.hw.leds_off()                              # LEDs off
        self.hw.motor_stop()                            # motor off
        if self.logger:                                 # if a case file is still open ...
            self.logger.close()                         # ... close it so everything is written

    # ================================================================================== sending
    def _send(self, body: str) -> None:                 # _send(): every outgoing frame goes through this one function
        self.link.send(body)                             # all frames leave through here

    def _send_stat(self) -> None:                       # _send_stat(): build and send the status frame
        """STAT,<state>,<cart>,<pwr>,<batt>  - tells the display what we are doing."""
        self._last_stat = self.clock()                   # remember when (for the 1 s rhythm)
        self._send(proto.stat(STAT_CODE[self.state],    # frame STAT with: state code,
                              1 if self.hw.cartridge_present() else 0,  # cartridge present (1/0),
                              1 if self.hw.on_battery() else 0,  # running on battery (1/0),
                              self.hw.battery_percent()))  # battery percent (or -1 if unknown)

    def _set_state(self, new: State) -> None:           # _set_state(): the only place where the state changes
        """Change state, log it, and tell the display immediately (do not wait for the 1 s rhythm)."""
        if new is not self.state:                       # only log real changes
            log.info("state %s -> %s", self.state.value, new.value)  # e.g. 'state IDLE -> BASELINE'
        self.state = new                                # store the new state
        self._send_stat()                               # tell the display at once (it also gets STAT every second)

    # ================================================================================== messages from the display
    def _on_msg(self, msg) -> None:                     # dispatch a message from the display
        """Dispatch a message from the display."""
        if msg.type == "PING":                           # heartbeat: nothing to do (STAT is sent anyway)
            return                                      # nothing to do for a heartbeat
        if msg.type == "BASE":                          # display asks for the baseline
            self._cmd_base()                            # start it
        elif msg.type == "CASE":                        # display opens a case
            self._cmd_case(msg.args)                    # pass the arguments (id, Hb)
        elif msg.type == "ENDC":                        # display closes the case
            self._cmd_endc()                            # close it
        else:                                           # anything else: ignore (forward compatible)
            log.debug("ignoring unknown message %s", msg.type)   # forward compatible: unknown types are harmless

    def _cmd_base(self) -> None:                        # _cmd_base(): handle the BASE message
        """BASE: run the optical baseline."""
        if self.state is State.SHUTDOWN:                # we are leaving: do nothing
            return                                      # nothing to do
        if self.state is State.BASELINE:                 # the display re-sends BASE every 2 s until it sees progress
            log.debug("BASE repeat ignored (baseline already running)")  # the display repeats BASE until it sees progress
            return                                      # so ignore the repeat
        if self.state is State.CASE_ACTIVE:              # the display never does this; protect an open case
            log.warning("BASE ignored: a case is active")  # protect the open case from a stray BASE
            return                                      # ignore
        if self.state is State.ERROR:                    # BASE also acts as 'try again'
            if not self._try_recover() or self.state is State.CASE_ACTIVE:   # still broken, or we are back in an open case
                return                                  # still broken, or we returned into an open case
        if self.clock() - self.baseline_done_at < self.cfg.base_dup_window_s:  # BASE arrived right after a baseline ended = stale repeat
            log.debug("BASE ignored: stale repeat of a baseline that just finished")  # ignore it
            return                                      # ignore it
        if not self.hw.cartridge_present():              # no cartridge = nothing to measure
            self._send(proto.err(1))                    # tell the display: no cartridge
            self._send(proto.bdon(False, 0, 0))          # so the display ends its baseline step
            return                                      # stop here
        self.baseline_ref = None                         # the old reference is no longer valid
        self._last_pct = -1                             # reset the progress counter
        self._set_state(State.BASELINE)                 # STAT 1 goes out immediately
        self._send(proto.bprg(0))                        # first progress frame right away: the display stops re-sending BASE
        self._start_job("baseline", lambda cancel, jid: run_baseline(  # run the baseline in a worker thread
            self.hw, self.cfg, cancel,                  # needs: hardware, settings, cancel flag,
            lambda pct: self.post("JOB_PROGRESS", (jid, pct))))   # the worker reports percent through the queue

    def _cmd_case(self, args: List[str]) -> None:       # _cmd_case(): handle CASE,<id>,<hb>
        """CASE,<id>,<hb>: open a case (or resume the open one)."""
        if self.state in (State.SHUTDOWN, State.BASELINE, State.ERROR) or len(args) != 2:  # wrong moment or wrong number of arguments
            log.debug("CASE ignored in state %s", self.state.value)  # write the reason to the debug log
            return                                      # ignore the message
        hb = valid_case(args[0], args[1])                # check id and Hb
        if hb is None:                                  # id or Hb is not valid
            log.warning("CASE rejected: invalid id or Hb %s", args)  # log and ignore
            return                                      # stop
        case_id = args[0]                               # first argument is the case id
        if self.case_id == case_id:                      # a repeat of the CASE we already have (display re-sends every 3 s)
            if self.state is not State.CASE_ACTIVE and self.hw.cartridge_present():  # the display re-sent the CASE we already have
                self.pad_ctx = PadContext()              # new/re-inserted cartridge: nothing was measured yet
                self._set_state(State.CASE_ACTIVE)       # resume after the cartridge was removed
            else:                                       # already active: just answer quickly
                self._send_stat()                        # already active: just confirm quickly
            return                                      # done
        if not self.hw.cartridge_present():             # a new case needs a cartridge
            self._send(proto.err(1))                    # tell the display
            return                                      # stop
        if self.case_id is not None:                     # a different case is still open: the ENDC must have been lost
            log.warning("new case %s while %s is open: closing the old one", case_id, self.case_id)  # tell the engineer
            self._close_case("closed by a new CASE")    # write END into the old file
        saved = self.store.load()                        # resume the pad counter after a reboot of the Pi
        resumed = bool(saved and saved["case_id"] == case_id)  # True when this id matches the saved unfinished case
        self.case_id, self.hb = case_id, hb             # remember id and Hb
        self.pad_n = saved["pad_n"] if resumed else 0   # continue the pad counter after a reboot, otherwise start at 0
        self.total_ml = 0.0                             # reset the running total
        self.logger = CaseLogger(self.cfg.data_dir / "cases", case_id, hb)  # open <data_dir>/cases/<id>.csv for appending
        self.logger.event("RESUME" if resumed else "START", "pad_n=%d" % self.pad_n)  # first row: START (or RESUME)
        if self.pending_baseline:                        # copy the baseline result into the case file
            self.logger.baseline(*self.pending_baseline)  # copy the baseline numbers into the case file
        elif self.baseline_ref is None:                 # no baseline was done before this case
            self.logger.event("NOTE", "case opened without a baseline")  # note it so the log is honest
        self.store.save(case_id, hb, self.pad_n)         # remember the open case
        self.pad_ctx = PadContext()                     # no half-measured pad at the start
        self._set_state(State.CASE_ACTIVE)              # STAT 2 goes out immediately

    def _cmd_endc(self) -> None:                        # _cmd_endc(): handle ENDC
        """ENDC: close the case, reset the baseline, go idle."""
        if self.state is State.SHUTDOWN:                # we are leaving: do nothing
            return                                      # nothing to do
        self._cancel_job()                               # stop any measurement and the motor
        if self.case_id is not None:                    # a case is open
            self._close_case("pads=%d total_ml=%.1f" % (self.pad_n, self.total_ml))  # write END with the pad count and total
        self.baseline_ref = None                         # baseline must be repeated for the next case
        self.pending_baseline = None                    # no baseline result is carried to the next case
        self.error_code = 0                             # no error remains
        self._resend.clear()                             # nothing left to repeat
        self._set_state(State.IDLE)                     # back to IDLE, STAT 0 goes out

    def _close_case(self, note: str) -> None:           # _close_case(): finish the open case
        """Write the END row, forget the open case."""
        if self.logger:                                 # if a file is open ...
            self.logger.event("END", note)              # ... write the END row ...
            self.logger.close()                         # ... and close it
        self.logger = None                              # no file any more
        self.store.clear()                               # no unfinished case any more
        self.case_id = None                             # no open case any more
        self.pad_n = 0                                  # pad counter back to zero
        self.pad_ctx = PadContext()                     # forget the pad progress

    # ================================================================================== START button
    def _on_button(self, _data=None) -> None:           # _on_button(): START was pressed
        """START pressed: measure a pad (when a case is open) or retry after an error."""
        if self.state is State.SHUTDOWN:                # we are leaving: ignore
            return                                      # ignore
        if self.state is State.ERROR:                    # START doubles as "try again"
            if not self._try_recover():                 # cause not fixed yet: stay in ERROR
                return                                  # nothing more to do
        if self.state is not State.CASE_ACTIVE:          # no case open on the display yet
            log.info("START ignored: no active case (create a case on the display first)")  # log why nothing happens
            return                                      # ignore the press
        if self.job_thread is not None and self.job_thread.is_alive():  # a pad job is still running
            log.info("START ignored: a measurement is already running")  # ignore double presses
            return                                      # nothing to do
        if not self.hw.cartridge_present():             # gauze needs a cartridge
            self._send(proto.err(1))                    # tell the display
            return                                      # stop
        log.info("START: measuring pad %d", self.pad_n + 1)  # log the start of a measurement
        ctx, hb, ref = self.pad_ctx, self.hb, self.baseline_ref   # snapshot for the worker thread
        self._start_job("pad", lambda cancel, jid: run_pad(self.hw, self.cfg, cancel, ctx, hb, ref))  # run the pad job in a worker thread

    # ================================================================================== jobs (worker threads)
    def _start_job(self, kind: str, fn: Callable[[threading.Event, int], Any]) -> None:  # _start_job(): run a long job in its own thread
        """Run fn(cancel, job_id) in a new thread and post its outcome as an event."""
        self._cancel_job()                               # never two jobs at once
        self.job_id += 1                                # new job number
        jid = self.job_id                                # identifies THIS job
        cancel = threading.Event()                      # a fresh cancel flag for this job
        self.job_cancel, self.job_kind = cancel, kind   # remember the flag and the kind

        def worker() -> None:                           # the function the new thread runs
            try:                                        # do the job
                result = fn(cancel, jid)                 # the long, blocking work
            except Cancelled:                           # the job was cancelled on purpose
                log.info("%s job cancelled", kind)       # nobody needs an answer
            except JobError as exc:                     # a known failure with an ERR code
                self.post("JOB_FAIL", (jid, kind, exc.code, exc.message))  # report it to the controller
            except Exception as exc:                     # any other bug
                log.exception("job crashed")            # write the traceback
                self.post("JOB_FAIL", (jid, kind, 2, "internal error: %r" % exc))  # report it as sensor fault 2
            else:                                       # no exception: the job succeeded
                self.post("JOB_DONE", (jid, kind, result))  # report the result to the controller

        self.job_thread = threading.Thread(target=worker, name="job-" + kind, daemon=True)  # create the thread (daemon: dies with the program)
        self.job_thread.start()                         # start it

    def _cancel_job(self) -> None:                      # _cancel_job(): stop the running job
        """Stop the running job (if any), wait for it, and make sure LEDs and motor are off."""
        if self.job_thread is not None and self.job_thread.is_alive():  # is a job running?
            self.job_cancel.set()                        # ask it to stop
            self.job_thread.join(timeout=3.0)            # it wakes up immediately from any wait
        self.job_thread = None                          # forget the thread
        self.job_id += 1                                 # results of the old job are now "stale" and will be ignored
        self.hw.leds_off()                               # belt and braces
        self.hw.motor_stop()                            # motor off

    def _on_job_progress(self, data) -> None:           # _on_job_progress(): baseline progress report
        """Baseline progress -> BPRG frame (only increasing values)."""
        jid, pct = data                                 # unpack the report
        if jid != self.job_id or self.state is not State.BASELINE or pct <= self._last_pct:  # ignore stale or backwards values
            return                                      # stop
        self._last_pct = pct                            # remember the percentage
        self._send(proto.bprg(pct))                     # send BPRG to the display

    def _on_job_done(self, data) -> None:               # _on_job_done(): a job finished
        """A job finished successfully."""
        jid, kind, result = data                        # unpack: job number, kind, result
        if jid != self.job_id:                           # an old, cancelled job: ignore
            return                                      # result of a cancelled job: ignore
        self.job_thread = None                          # the thread has finished
        if kind == "baseline":                          # baseline or pad?
            self._baseline_finished(result)             # handle the baseline result
        else:                                           # otherwise it was a pad
            self._pad_finished(result)                  # handle the pad result

    def _baseline_finished(self, res: BaselineResult) -> None:  # _baseline_finished(): baseline result arrived
        """BDON and the next state."""
        self.baseline_done_at = self.clock()            # remember when (to ignore stale BASE repeats)
        self._send(proto.bdon(res.ok, res.i540, res.i850))  # tell the display: BDON,<ok>,<i540>,<i850>
        if res.ok:                                      # baseline passed
            self.baseline_ref = res.reading              # keep as reference for pad blanks
            self.pending_baseline = (True, res.i540, res.i850, "")  # copy into the case file later
            log.info("baseline OK (i540=%d i850=%d)", res.i540, res.i850)  # log the numbers
            self._log_usable_range(res.reading)         # tell the engineer how many mL one pad may hold
            self._set_state(State.READY)                # STAT 0: ready for a case
        else:                                           # baseline failed
            self.baseline_ref = None                    # no reference available
            self.pending_baseline = None                # nothing to write
            log.warning("baseline FAILED: %s", res.message)  # log the reason
            self._set_state(State.IDLE)                  # the display lets the operator try again

    def _log_usable_range(self, ref: Reading) -> None:  # _log_usable_range(): design check (see docstring)
        """Log the biggest pad the optics can measure with the current calibration (a design check).

        The sample must stay brighter than cfg.min_signal counts, so the largest usable absorbance is
        log10(blank / min_signal) (and never more than cfg.max_absorbance).
        """
        import math
        a_max = min(self.cfg.max_absorbance, math.log10(ref.net540 / self.cfg.min_signal))  # largest usable absorbance (limited by the weakest signal we accept)
        ml = (self.cfg.cal_slope_ml_per_a * a_max + self.cfg.cal_intercept_ml)   # mL at the calibration Hb
        log.info("usable range: absorbance up to %.2f = about %.1f mL per pad at Hb %.1f g/dL. "  # write one information line to the log:
                 "A bigger pad gives ERR 4 (over-range): increase the reagent volume or the LED current.",  # what happens with a bigger pad
                 a_max, ml, self.cfg.cal_hb_ref)        # the three values filled into the text above

    def _pad_finished(self, out: PadOutcome) -> None:   # _pad_finished(): a pad result arrived
        """Log the pad, send PAD, make sure the display really gets it."""
        r = out.result                                  # r = the calculated result (A540, net A, mL ...)
        self.pad_n += 1                                  # pad numbers: 1, 2, 3 ...
        self.total_ml += r.ebl_ml                       # add to the running total
        if self.logger:                                 # if a case file is open ...
            self.logger.pad(self.pad_n, out.blank.dark, out.blank.net540, out.blank.net850,  # ... write all raw numbers of this pad ...
                            out.sample.net540, out.sample.net850, r.a540, r.a850, r.net_a, r.ebl_ml, r.flags)  # ... so the result can be re-calculated later
        self.store.save(self.case_id or "", self.hb, self.pad_n)   # remember the counter (survives a reboot)
        self.pad_ctx = PadContext()                      # the next pad starts from scratch
        body = proto.pad(self.pad_n, r.ebl_ml)          # frame text, e.g. PAD,3,41.2
        self._send(body)                                 # the important frame
        # No acknowledgement exists in the protocol, so we repeat the frame a few times. The display ignores
        # duplicate pad numbers, so repeating is harmless but protects against a lost frame.
        self._resend.append([body, self.cfg.pad_resend_count, self.clock() + self.cfg.pad_resend_period_s])  # schedule 3 repeats (see comment above)
        log.info("pad %d = %.1f mL (total %.1f mL) %s", self.pad_n, r.ebl_ml, self.total_ml, " ".join(r.flags))  # one line in the log
        self._send_stat()                               # STAT right away

    def _on_job_fail(self, data) -> None:               # _on_job_fail(): a job failed with an ERR code
        """A job failed with a protocol error code."""
        jid, kind, code, message = data                 # unpack the report
        if jid != self.job_id:                          # stale job?
            return                                      # ignore
        self.job_thread = None                          # the thread has finished
        if kind == "baseline":                          # baseline or pad?
            self._send(proto.bdon(False, 0, 0))          # the display must not wait forever
            self._enter_error(code, message, State.IDLE)  # failed baseline: go to ERROR, come back to IDLE
        else:                                           # otherwise a pad failed
            self._enter_error(code, message, State.CASE_ACTIVE)   # pad_ctx is kept: START resumes where it stopped

    # ================================================================================== errors
    def _enter_error(self, code: int, message: str, return_to: State) -> None:  # _enter_error(): go to the ERROR state
        """Go to ERROR: remember where to come back to (history state), tell the display."""
        log.error("ERROR %d: %s", code, message)        # log the error
        self.hw.leds_off()                              # LEDs off
        self.hw.motor_stop()                            # motor off
        self.error_code = code                          # remember the code
        self.return_state = return_to if return_to in (State.IDLE, State.READY, State.CASE_ACTIVE) else State.IDLE  # remember where to return after the fix (only legal states)
        if self.logger:                                 # if a case file is open ...
            self.logger.event("ERROR", "code=%d %s" % (code, message))  # ... note the error in it
        self._set_state(State.ERROR)                    # STAT 3 goes out
        self._send(proto.err(code))                     # ERR,<code> goes out
        self._last_err = self.clock()                   # remember when (for the repeat every few seconds)

    def _try_recover(self) -> bool:                     # _try_recover(): leave ERROR when the cause is gone
        """Leave ERROR if the cause is gone.  Returns True when we are back in a normal state."""
        if not self.hw.cartridge_present():             # is the cartridge there?
            self._send(proto.err(1))                     # still no cartridge
            return False                                # no: stay in ERROR
        if self.error_code in (2, 4) and not self.hw.self_test():  # after a sensor fault, is the ADC answering again?
            self._send(proto.err(2))                     # ADC still not answering
            return False                                # no: stay in ERROR
        log.info("error %d cleared -> %s", self.error_code, self.return_state.value)  # log the recovery
        self.error_code = 0                             # no error any more
        self._set_state(self.return_state)              # go back to where we were (history state)
        return True                                     # success

    # ================================================================================== periodic work (every 0.25 s)
    def _on_poll(self, _data=None) -> None:             # _on_poll(): runs every 0.25 s
        """Check inputs, send STAT once per second, repeat frames."""
        now = self.clock()                              # current time
        self.last_poll = now                             # proves to the watchdog that this loop is alive
        cart = self.hw.cartridge_present()              # read the cartridge switch
        if cart != self._last_cart:                      # the cartridge was inserted or removed
            self._cartridge_changed(cart)               # react to the change
            self._last_cart = cart                      # remember the new value
        if self.hw.power_fail():                         # the power manager says: power is about to fail
            self._shutdown("power-fail warning")        # power is about to fail: stop safely
            return                                      # nothing more to do
        if now - self._last_stat >= self.cfg.stat_period_s:  # time for the 1 s heartbeat?
            self._send_stat()                            # the heartbeat the display waits for
        for item in list(self._resend):                  # repeat PAD frames
            if now >= item[2]:                          # time to repeat this frame?
                self._send(item[0])                     # send it again
                item[1] -= 1                            # one repeat fewer
                item[2] = now + self.cfg.pad_resend_period_s  # when to send the next repeat
                if item[1] <= 0:                        # all repeats done
                    self._resend.remove(item)           # remove it from the list
        if self.state is State.ERROR and now - self._last_err >= self.cfg.error_repeat_period_s:  # ERROR: repeat the ERR frame every few seconds
            self._send(proto.err(self.error_code))       # keep the error visible
            self._last_err = now                        # remember the time
            if self.error_code == 1 and cart:            # the missing cartridge is back: error clears by itself
                self._try_recover()                     # try to leave ERROR

    def _cartridge_changed(self, present: bool) -> None:  # _cartridge_changed(): inserted or removed
        """React to a cartridge being inserted or removed."""
        log.info("cartridge %s", "inserted" if present else "removed")  # log it
        if present:                                     # inserted
            if self.state is State.ERROR and self.error_code == 1:  # an 'ERR 1 no cartridge' error ...
                self._try_recover()                     # ... is cleared by inserting it
            return                                      # done
        # removed:
        if self.state in (State.BASELINE, State.READY, State.CASE_ACTIVE):  # removed during a session
            self._cancel_job()                           # stop the motor/LEDs at once
            self.baseline_ref = None                     # baseline belongs to the old cartridge
            self.pending_baseline = None                # the old baseline belongs to the old cartridge
            self.pad_ctx = PadContext()                  # a half-measured pad is lost with the cartridge
            if self.logger:                             # if a case file is open ...
                self.logger.event("CARTRIDGE_REMOVED")  # ... note it
            self._set_state(State.IDLE)                  # the open case (if any) is kept; CASE resumes it
