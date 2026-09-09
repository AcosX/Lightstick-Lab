import tempfile
import threading
import unittest
from pathlib import Path
from lightstick_demo.engine import Engine
from lightstick_demo.model import LogicalUpdate, LogicalState, RadioSettings
from lightstick_demo.protocols.registry import ProtocolRegistry
from lightstick_demo.protocol import build_d8_transaction, build_partition_frame
from lightstick_demo.scheduler import Scheduler
from lightstick_demo.state import StateStore
from test_cli_control import FakeTransport

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name))
        self.engine = Engine(self.store, protocol_id='protocol_d8')
        self.engine.transport = FakeTransport('test')
    def tearDown(self):
        self.engine.stop()
        self.directory.cleanup()
    def test_golden_d8_and_short(self):
        update = LogicalUpdate(('B',), (6,12,15), 'medium')
        plan = self.engine.preview(update)
        slots = [0xF00, 0x26CF] + [0]*7
        self.assertEqual(plan.commands[0][1]['durations_us'], list(build_d8_transaction(slots).durations_us))
        self.assertEqual([f[-2] for f in plan.frames], [2,1,0,2,1,0])
        self.engine.select_protocol('protocol_00')
        plan = self.engine.preview(LogicalUpdate(('A','P'), (15,0,0), 'solid', 0))
        self.assertEqual(plan.frames, (build_partition_frame(1,128),))
    def test_commit_and_protocol_restore(self):
        self.engine.execute_update(LogicalUpdate(('B',),(6,12,15)))
        self.engine.select_protocol('protocol_00')
        self.engine.select_protocol('protocol_d8')
        plan = self.engine.preview(LogicalUpdate(('A',),(0,15,0)))
        self.assertEqual(plan.next_state['slots'][1], 0x6CF)
    def test_old_state_migration_is_idempotent(self):
        self.store.path.write_text('{"d8_slots":[1,2,3,4,5,6,7,8,9]}')
        state = self.store.load_unlocked()
        self.store.save_unlocked(state)
        self.assertEqual(self.store.load_unlocked(), state)
        self.assertEqual(state['protocol_states']['protocol_d8']['slots'], list(range(1,10)))
    def test_invalid_capability_no_tx(self):
        with self.assertRaisesRegex(ValueError, 'unsupported_zone = J'):
            self.engine.submit_update(LogicalUpdate(('J',),(1,2,3)))
        self.assertEqual(self.engine.status()['tx']['unsupported'], 1)
        self.assertEqual(self.engine.transport.calls, [])
    def test_failed_tx_retains_state(self):
        from lightstick_demo.transports import TransportError
        self.engine.transport = FakeTransport('test', responses=[TransportError('failed')])
        before = self.store.load_unlocked()
        with self.assertRaises(TransportError):
            self.engine.execute_update(LogicalUpdate(('B',),(1,2,3)))
        self.assertEqual(self.store.load_unlocked(), before)

class SchedulerTests(unittest.TestCase):
    def test_single_tx_latest_state_and_dedupe(self):
        started = threading.Event(); release = threading.Event(); sent = []
        def execute(state):
            sent.append(state)
            started.set()
            release.wait(2)
        scheduler = Scheduler(execute); scheduler.start()
        try:
            scheduler.submit((LogicalUpdate(('A',),(1,0,0)),))
            self.assertTrue(started.wait(2))
            for value in range(2,16):
                scheduler.submit((LogicalUpdate(('B',),(value,0,0)),))
                self.assertEqual(scheduler.status()['pending'], 1)
            release.set()
            self.assertTrue(scheduler.wait_idle(2))
            self.assertEqual(len(sent),2)
            self.assertEqual(dict(sent[-1].values)['B'].rgb, (15,0,0))
            self.assertFalse(scheduler.submit((LogicalUpdate(('B',),(15,0,0)),)))
        finally:
            release.set(); scheduler.stop()
