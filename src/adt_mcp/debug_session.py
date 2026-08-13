"""One debug session per system: a background listener plus the attached debuggee.

Why the listener has to run in the background: a debuggee is only caught if the
listener is already registered *before* the code runs. If the listen tool
blocked until something arrived, the agent would be stuck inside that call and
could never reach run_class — it would deadlock itself.
"""
import threading
import time

from . import debugger as dbg
from .debug_pool import CONTROL, DEBUG, EXEC, DebugError, DebugSessionPool, key
from .registry import System

# Length of one long-poll round. stop() only takes effect once the round in
# flight returns, so this is also the worst-case wait when stopping.
_POLL_CHUNK = 30

# At shutdown the current long-poll round has to be allowed to finish: a shorter
# join leaves the background thread holding a DebugSession while we use that
# same session to clean up — two threads, one session, which the pool forbids.
_CLOSE_JOIN = _POLL_CHUNK + 5

# start_listen only returns once the background thread has actually issued the
# registration request. Returning sooner means the agent runs code while SAP has
# no listener to trap it — the breakpoint stays silent, which is exactly the
# "I set a breakpoint and nothing happened" symptom.
_LISTEN_ARMED = 5.0

# How long run_class waits before reporting "still running". Long enough for an
# ordinary class, short enough that the agent is not left hanging.
_RUN_WAIT = 60.0

# Poll interval while waiting for the background run. Small enough to feel
# instant, large enough not to spin a core for minutes.
_RUN_POLL = 0.1


class _State:
    def __init__(self, system: System | None = None):
        # Kept so shutdown can still build a session to delete breakpoints —
        # _states is keyed by system name alone.
        self.system = system
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        # Set immediately before each long poll: "the listener is on the air".
        self.polling = threading.Event()
        self.debuggee: dict | None = None
        self.attached = False
        self.error = ""
        self.timed_out = False
        # True between accepting a listen command and the thread actually running.
        self.starting = False
        self.run_thread: threading.Thread | None = None
        self.run_text: str | None = None
        self.run_lock = threading.Lock()
        # True while run_and_wait is waiting for its own run. The lock only
        # prevents corruption, not theft: without this flag a poll landing at
        # the wrong moment takes the result and run_class reports "still
        # running" for a run that finished long ago.
        self.run_waiting = False
        # Ids of the breakpoints this server created. They have to be
        # remembered: there is no list-breakpoints call over REST, so this is
        # the only way to clean up. A forgotten external breakpoint pops the
        # debugger open on a real session later.
        self.breakpoints: list[str] = []


class DebugManager:
    """At most ONE debug session per system. Asking for a second is an error,
    not a silent takeover of the one already running."""

    def __init__(self, pool: DebugSessionPool):
        self._pool = pool
        self._states: dict[str, _State] = {}
        self._guard = threading.Lock()

    def _state(self, system: System) -> _State:
        with self._guard:
            st = self._states.get(system.name)
            if st is None:
                st = self._states[system.name] = _State(system)
            elif st.system is None:
                st.system = system
            return st

    def _listening(self, st: _State) -> bool:
        if st.starting:
            return True
        return st.thread is not None and st.thread.is_alive()

    def user(self, system: System) -> str:
        """The ABAP user this system debugs as (from the tenant, not config)."""
        return self._pool.identity(system)["userName"]

    # --- listener ---

    def start_listen(self, system: System, seconds: int = 120) -> str | None:
        """None on success, otherwise the error to show."""
        st = self._state(system)
        # Check-then-create must be inside one lock: two concurrent calls could
        # both pass the check and start two threads, the first becoming a zombie
        # that overwrites the second one's state.
        with self._guard:
            if self._listening(st):
                return (f"Error: system {system.name!r} is already listening — "
                        f"call debug_poll, or debug_stop_listener")
            if st.attached:
                return (f"Error: system {system.name!r} is attached to a "
                        f"debuggee — call debug_detach first")
            st.stop.clear()
            st.polling.clear()
            st.debuggee = None
            st.error = ""
            st.timed_out = False
            st.starting = True

        # Built here, not in the thread: on a cookie system this may have to log
        # in, and a failure has to reach the caller as an error rather than
        # disappear into a background thread.
        try:
            user = self.user(system)
            session = self._pool.session(system, DEBUG)
        except DebugError as e:
            st.starting = False
            return str(e)

        lock = self._pool.lock(system, DEBUG)

        def loop():
            st.starting = False
            remaining = max(1, int(seconds))
            try:
                while remaining > 0 and not st.stop.is_set():
                    chunk = min(_POLL_CHUNK, remaining)
                    with lock:
                        st.polling.set()
                        caught = dbg.listen(session, user, seconds=chunk)
                    if caught:
                        st.debuggee = caught
                        return
                    remaining -= chunk
                if not st.stop.is_set():
                    st.timed_out = True
            except Exception as e:  # noqa: BLE001
                st.error = f"{type(e).__name__}: {e}"
            finally:
                # Release start_listen if the thread died before its first
                # registration, instead of making it wait out _LISTEN_ARMED and
                # then report success for a listener that is already dead.
                st.polling.set()

        st.thread = threading.Thread(target=loop, daemon=True,
                                     name=f"adtmcp-debug-{system.name}")
        st.thread.start()
        # Wait until the registration request has really gone out. This is a
        # correctness condition, not an optimisation: an external breakpoint
        # only fires once SAP holds a listener for this exact
        # (user, terminalId, ideId).
        st.polling.wait(timeout=_LISTEN_ARMED)
        return None

    def poll(self, system: System) -> dict:
        """Session state: idle | listening | caught | attached | timeout | error.

        Carries 'running' (a background run is still in flight) and 'output'
        (the finished run nobody has collected yet) — after debug_detach, the
        output is only available here.
        """
        st = self._state(system)
        extra = {"running": self.is_running(system),
                 "output": self.take_run_output(system)}
        if st.error:
            return {"state": "error", "message": st.error, "debuggee": None,
                    **extra}
        if st.attached:
            return {"state": "attached", "message": "", "debuggee": st.debuggee,
                    **extra}
        if self._listening(st):
            return {"state": "listening", "message": "", "debuggee": None,
                    **extra}
        if st.debuggee:
            return {"state": "caught", "message": "", "debuggee": st.debuggee,
                    **extra}
        if st.timed_out:
            return {"state": "timeout", "message": "", "debuggee": None, **extra}
        return {"state": "idle", "message": "", "debuggee": None, **extra}

    def stop(self, system: System) -> None:
        """Stop the listener. Takes effect after the current poll round ends."""
        st = self._state(system)
        st.stop.set()
        try:
            with self._pool.lock(system, CONTROL):
                dbg.stop_listener(self._pool.session(system, CONTROL),
                                  self.user(system))
        except DebugError:
            pass
        if st.thread is not None:
            st.thread.join(timeout=_CLOSE_JOIN)
        st.thread = None
        st.timed_out = False

    # --- attached session ---

    def attach(self, system: System) -> str | None:
        st = self._state(system)
        if self._listening(st):
            return (f"Error: the listener on {system.name!r} is still waiting — "
                    f"call debug_poll until state='caught'")
        if not st.debuggee:
            return (f"Error: nothing has been caught on {system.name!r} — call "
                    f"debug_listen and then run the code")
        if not st.debuggee.get("attachable", True):
            return "Error: SAP says this debuggee cannot be attached"
        with self._pool.lock(system, DEBUG):
            dbg.attach(self._pool.session(system, DEBUG), st.debuggee["id"],
                       self.user(system))
        st.attached = True
        return None

    def require_attached(self, system: System) -> str | None:
        st = self._state(system)
        if not st.attached:
            return (f"Error: not attached to a debuggee on {system.name!r} — "
                    f"call debug_attach first")
        return None

    def run_attached(self, system: System, fn):
        """Run fn(session) on the debug session itself (stack/variables/step).

        Stateful calls only. Everything else goes through another channel.
        """
        with self._pool.lock(system, DEBUG):
            return fn(self._pool.session(system, DEBUG))

    def run_control(self, system: System, fn):
        """Run fn(session) on the control channel (set/delete breakpoints).

        Setting a breakpoint is a STATELESS call. Letting it ride the debug
        channel would kill your own debug session; letting it ride the exec
        channel would queue it behind the program stopped at the breakpoint —
        and deleting a breakpoint is exactly what you want to do right then.
        """
        with self._pool.lock(system, CONTROL):
            return fn(self._pool.session(system, CONTROL))

    # --- execution ---

    def exec_timeout(self, system: System) -> float:
        """HTTP ceiling for running code, depending on whether we are debugging.

        While debugging it widens to `debug_timeout`: code stopped at a
        breakpoint holds the very HTTP request that started it, so an ordinary
        ceiling would cut the connection while you are still reading variables,
        killing the background thread and losing the run's output.

        When not debugging it stays ordinary. Widening it for every run means a
        hung program keeps one of the tenant's work processes for many minutes
        with nobody watching.
        """
        st = self._state(system)
        if self._listening(st) or st.debuggee or st.attached:
            return float(getattr(system, "debug_timeout", 600) or 600)
        return 60.0

    def run_and_wait(self, system: System, fn) -> dict:
        """Run fn(session) in the BACKGROUND on the exec channel, then wait.

        Returns {"state": …} where state is one of:
          done     — finished, carries "text"
          caught   — the code is stopped at a breakpoint, carries "debuggee"
          running  — still going after the wait ceiling (call debug_poll)
          busy     — another run on this system has not finished
          blocked  — already stopped at an earlier debuggee

        Why background: when the code stops at a breakpoint, SAP holds the HTTP
        request that started it — the call only returns once the debuggee is
        released. Running it inline would hang this very tool, so the agent
        could never call debug_attach to release it: the same self-deadlock
        debug_listen avoids.

        fn(session) must RETURN A STRING and must not raise (the tool layer
        wraps it) — it runs on another thread, where an exception would never
        reach the agent.
        """
        st = self._state(system)
        with self._guard:
            if st.run_thread is not None and st.run_thread.is_alive():
                return {"state": "busy"}
            if st.attached or st.debuggee:
                return {"state": "blocked", "debuggee": st.debuggee}
            st.run_text = None
            st.run_waiting = True
            st.run_thread = thread = threading.Thread(
                target=lambda: self._run_body(system, st, fn), daemon=True,
                name=f"adtmcp-run-{system.name}")
        thread.start()

        deadline = time.monotonic() + _RUN_WAIT
        try:
            while True:
                with st.run_lock:
                    text, st.run_text = st.run_text, None
                if text is not None:
                    return {"state": "done", "text": text}
                if st.debuggee:
                    return {"state": "caught", "debuggee": st.debuggee}
                if time.monotonic() >= deadline:
                    return {"state": "running"}
                time.sleep(_RUN_POLL)
        finally:
            st.run_waiting = False

    def _run_body(self, system: System, st: _State, fn) -> None:
        try:
            with self._pool.lock(system, EXEC):
                text = fn(self._pool.session(system, EXEC))
        except Exception as e:  # noqa: BLE001
            text = f"Error: run failed on {system.name!r}: {type(e).__name__}: {e}"
        with st.run_lock:
            st.run_text = text if isinstance(text, str) else str(text)

    def take_run_output(self, system: System, wait: float = 0.0) -> str:
        """The last background run's output, consumed. "" if there is none.

        Waits up to `wait` seconds: called right after releasing a debuggee, the
        code still has to finish the part after the breakpoint before there is
        any output.

        Yields while run_and_wait is waiting for that same run — otherwise a
        poll at the wrong moment takes the result and run_class reports a
        finished run as "still running". Not data loss, but a wrong answer, and
        the agent would keep waiting for nothing.
        """
        st = self._state(system)
        # Join OUTSIDE the lock: _run_body needs that same lock to publish the
        # result, so holding it while waiting would deadlock.
        thread = st.run_thread
        if wait > 0 and thread is not None:
            thread.join(timeout=wait)
        with st.run_lock:
            if st.run_waiting:
                return ""
            if st.run_thread is not None and not st.run_thread.is_alive():
                st.run_thread = None
            text, st.run_text = st.run_text or "", None
        return text

    def is_running(self, system: System) -> bool:
        st = self._state(system)
        return st.run_thread is not None and st.run_thread.is_alive()

    # --- breakpoints we created ---

    def remember_breakpoints(self, system: System, ids: list[str]) -> None:
        """Remember ids so shutdown can delete them.

        Under the lock: two concurrent set_breakpoint calls with a non-atomic
        check-then-append would duplicate or drop entries, and a dropped id
        means an external breakpoint left behind.
        """
        st = self._state(system)
        with st.run_lock:
            for ident in ids:
                if ident and ident not in st.breakpoints:
                    st.breakpoints.append(ident)

    def forget_breakpoint(self, system: System, ident: str) -> None:
        st = self._state(system)
        with st.run_lock:
            if ident in st.breakpoints:
                st.breakpoints.remove(ident)

    def breakpoint_ids(self, system: System) -> list[str]:
        st = self._state(system)
        with st.run_lock:
            return list(st.breakpoints)

    # --- teardown ---

    def release(self, system: System) -> None:
        """Release the debuggee and clear the session state. Never raises.

        Confirmed with is_attached rather than trusting a status code: the
        detach paths do not report through theirs.
        """
        st = self._state(system)
        st.stop.set()
        if st.thread is not None:
            st.thread.join(timeout=_CLOSE_JOIN)
        # Release even when merely caught but not yet attached: it is still
        # stopped and still holding a work process. That is the most common
        # case — an agent that gave up between 'caught' and 'attach'.
        if st.attached or st.debuggee:
            try:
                with self._pool.lock(system, DEBUG):
                    session = self._pool.session(system, DEBUG)
                    dbg.detach(session)
                    if dbg.is_attached(session):
                        # Could not release it: the debug session is still
                        # alive. Drop the session entirely so the next attempt
                        # starts clean, rather than keep using one that is
                        # holding somebody's debuggee.
                        self._pool.invalidate(system.name, DEBUG)
            except Exception:  # noqa: BLE001
                pass
        st.thread = None
        st.debuggee = None
        st.attached = False
        st.error = ""
        st.timed_out = False

    def close_all(self) -> None:
        """Release every debuggee and delete every breakpoint we created.

        Order matters: delete breakpoints BEFORE releasing debuggees only in the
        sense that both need a usable session — breakpoints go through the
        control channel, which the debug channel's death cannot affect.
        """
        with self._guard:
            items = list(self._states.items())
        for name, st in items:
            st.stop.set()
            if st.thread is not None:
                st.thread.join(timeout=_CLOSE_JOIN)
            control = self._pool.get_existing(name, CONTROL)
            if control is None and st.breakpoints and st.system is not None:
                try:
                    control = self._pool.session(st.system, CONTROL)
                except Exception:  # noqa: BLE001
                    control = None
            if control is not None and st.breakpoints:
                # The ABAP user, never System.username — a breakpoint is keyed
                # on it, so deleting with the IAS e-mail would delete nothing
                # and leave the trap armed.
                try:
                    user = self._pool.identity(st.system)["userName"]
                except Exception:  # noqa: BLE001
                    user = ""
                with self._pool.lock_by_name(key(name, CONTROL)):
                    for ident in list(st.breakpoints):
                        try:
                            dbg.delete_breakpoint(control, ident, user)
                        except Exception:  # noqa: BLE001
                            pass
                    # Safety net on top of the per-id deletes: it also catches
                    # breakpoints left by an earlier run of this server, whose
                    # ids nobody recorded. Unverified, hence not a replacement.
                    try:
                        dbg.clear_breakpoints(control, user)
                    except Exception:  # noqa: BLE001
                        pass
            session = self._pool.get_existing(name, DEBUG)
            if session is not None and (st.attached or st.debuggee):
                with self._pool.lock_by_name(key(name, DEBUG)):
                    try:
                        dbg.detach(session)
                    except Exception:  # noqa: BLE001
                        pass
            # Releasing the debuggee lets the program finish, which is what
            # frees the background run thread — join AFTER the release.
            if st.run_thread is not None:
                st.run_thread.join(timeout=_CLOSE_JOIN)
                st.run_thread = None
            st.thread = None
            st.starting = False
            st.debuggee = None
            st.attached = False
            st.run_text = None
            st.error = ""
            st.timed_out = False
            st.breakpoints.clear()
        self._pool.close_all()
