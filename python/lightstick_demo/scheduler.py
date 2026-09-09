"""One RF worker, one replaceable pending state, no unbounded event queue."""
import threading
import time
from .model import LogicalState

class Scheduler:
    def __init__(self, execute):
        self.execute = execute
        self._condition = threading.Condition()
        self._thread = None
        self._stopping = False
        self._desired = LogicalState()
        self._confirmed = LogicalState()
        self._running = None
        self._pending = None
        self.last_error = ''
        self.counters = dict.fromkeys(('received','deduplicated','coalesced','unsupported','transmitted','failed'), 0)

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name='lightstick-scheduler', daemon=True)
            self._thread.start()

    def submit(self, updates):
        with self._condition:
            if self._stopping:
                raise RuntimeError('Scheduler is stopping')
            self.counters['received'] += 1
            desired = self._desired.apply(updates)
            if desired == self._desired and (self._pending is not None or self._running == desired or self._confirmed == desired):
                self.counters['deduplicated'] += 1
                return False
            self._desired = desired
            if self._pending is not None:
                self.counters['coalesced'] += 1
            self._pending = desired
            self._condition.notify_all()
            return True

    def unsupported(self, error):
        with self._condition:
            self.counters['unsupported'] += 1
            self.last_error = str(error)

    def status(self):
        with self._condition:
            return {**self.counters, 'running': self._running is not None,
                    'pending': int(self._pending is not None), 'error': self.last_error}

    def reset(self):
        with self._condition:
            if self._running is not None or self._pending is not None:
                raise RuntimeError('Cannot switch protocol while TX is pending')
            self._desired = self._confirmed = LogicalState()

    def wait_idle(self, timeout=45):
        end = time.monotonic() + timeout
        with self._condition:
            while self._running is not None or self._pending is not None:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self):
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()  # Controller has a bounded TX deadline; never disconnect mid-TX.
        self._thread = None

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._pending is not None)
                if self._stopping:
                    return
                target = self._pending
                self._pending = None
                if target == self._confirmed:
                    self.counters['deduplicated'] += 1
                    self._condition.notify_all()
                    continue
                self._running = target
                confirmed = dict(self._confirmed.values)
                changed = LogicalState(tuple((z,v) for z,v in target.values if confirmed.get(z) != v))
            try:
                self.execute(changed)
            except Exception as exc:
                with self._condition:
                    self.counters['failed'] += 1
                    self.last_error = str(exc)
            else:
                with self._condition:
                    self._confirmed = target
                    self.counters['transmitted'] += 1
                    self.last_error = ''
            finally:
                with self._condition:
                    self._running = None
                    self._condition.notify_all()
