"""Resource governor: the node enforces its own limits (the owner always comes first).

What is limited: the share of wall-clock time, over a rolling window, that this node spends on shared jobs (a "duty cycle").
It is NOT a CPU percentage, and a job that has started is allowed to finish; the node then rests until its average is back under the limit.
Owner priority: if this computer is busy with its owner's own work (load average per core above a threshold), it takes no shared work.
Windows has no load average here, so the owner-busy check is reported as 'not measured' there, never guessed.
"""
import os, threading, time


class Governor:
    def __init__(self, share_limit_pct=100.0, window_s=600.0, max_concurrent=1, busy_load=0.8, clock=time.time, load_fn=None):
        self.limit = float(share_limit_pct)
        self.window = float(window_s)
        self.max_concurrent = int(max_concurrent)
        self.busy_load = busy_load
        self.clock = clock
        self.load_fn = load_fn if load_fn is not None else self._load_per_core
        self.paused = False
        self.lock = threading.Lock()
        self.done = []        # finished jobs: (start, end)
        self.running = {}     # token -> start
        self._n = 0

    @staticmethod
    def _load_per_core():
        try:
            return os.getloadavg()[0] / (os.cpu_count() or 1)
        except (AttributeError, OSError):
            return None       # not measurable on this system

    def set_limit(self, pct):
        self.limit = max(0.0, min(100.0, float(pct)))

    def _used_s(self, now):
        lo, total = now - self.window, 0.0
        self.done = [(a, b) for a, b in self.done if b > lo]
        for a, b in self.done:
            total += max(0.0, min(b, now) - max(a, lo))
        for a in self.running.values():
            total += max(0.0, now - max(a, lo))
        return total

    def used_pct(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            return round(100.0 * self._used_s(now) / self.window, 1)

    def may_take(self):
        """-> (ok, reason, wait_seconds). Never raises."""
        now = self.clock()
        with self.lock:
            if self.paused:
                return False, "paused by the owner", 10.0
            if len(self.running) >= self.max_concurrent:
                return False, "already running its maximum number of shared jobs", 2.0
            load = self.load_fn()
            if load is not None and load >= self.busy_load:
                return False, "the owner is using this computer", 10.0
            cap = self.limit / 100.0 * self.window
            if self._used_s(now) >= cap:
                # find when enough busy time has aged out of the window
                wait = 1.0
                while wait < self.window and self._used_s(now + wait) >= cap:
                    wait += 1.0
                return False, f"share limit {self.limit:g}% reached", min(wait, 30.0)
            return True, "", 0.0

    def start(self):
        with self.lock:
            self._n += 1
            self.running[self._n] = self.clock()
            return self._n

    def end(self, token):
        with self.lock:
            a = self.running.pop(token, None)
            if a is not None:
                self.done.append((a, self.clock()))

    def report(self):
        ok, reason, _ = self.may_take()
        return {"share_limit_pct": self.limit, "used_pct": self.used_pct(), "taking_work": ok, "reason": reason,
                "owner_load_measured": self.load_fn() is not None}
