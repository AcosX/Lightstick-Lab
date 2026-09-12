"""Real Tk layout regression; skipped only when no display is available."""
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from lightstick_demo.ui import LightstickApp
from lightstick_demo.state import StateStore
from lightstick_demo.transports import TransportStatus
from test_cli_control import FakeTransport

class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        try:
            cls.app = LightstickApp(StateStore(Path(cls.directory.name)))
        except tk.TclError as exc:
            cls.directory.cleanup()
            raise unittest.SkipTest(f'Tk display unavailable: {exc}')
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app._close()
        cls.directory.cleanup()

    def test_controls_fit_at_minimum_and_default_size(self):
        app = self.app
        notebook = app.lightstick_tab.master
        for size in ('850x650', '900x650'):
            app.geometry(size)
            for name in app._protocol_names:
                app.air_family_var.set(name)
                for tab in (app.lightstick_tab, app.external_tab):
                    notebook.select(tab)
                    app.update()
                    def check(parent):
                        for child in parent.winfo_children():
                            if not child.winfo_ismapped():
                                continue
                            with self.subTest(size=size, protocol=name, widget=str(child)):
                                self.assertGreaterEqual(child.winfo_height(), child.winfo_reqheight())
                                self.assertLessEqual(child.winfo_y()+child.winfo_height(), parent.winfo_height())
                                self.assertLessEqual(child.winfo_x()+child.winfo_width(), parent.winfo_width())
                            check(child)
                    check(tab)
        notebook.select(app.lightstick_tab)

    def test_external_controls_have_separate_tab(self):
        app = self.app
        notebook = app.external_tab.master
        self.assertEqual([notebook.tab(t, 'text') for t in notebook.tabs()], ['应援棒','ESP32','外部控制'])
        self.assertEqual(len(app.lightstick_tab.winfo_children()), 6)
        self.assertEqual(len(app.external_tab.winfo_children()), 3)

    def test_enabled_effects_reach_fake_bridge(self):
        app = self.app
        fake = FakeTransport('layout-test')
        app.transport = app.engine.transport = fake
        app.transport_status = TransportStatus('test', True, 'fake')
        errors = []
        app._show_error = lambda *args: errors.append(args)
        for name in app._protocol_names:
            app.air_family_var.set(name)
            for label, button in app.function_buttons.items():
                if button.instate(['!disabled']):
                    app._send_state(label)
                    deadline = time.monotonic() + 2
                    while app._busy_tasks and time.monotonic() < deadline:
                        app.update()
                        time.sleep(.005)
                    self.assertFalse(app._busy_tasks)
                    self.assertFalse(app.engine.status()['tx']['error'])
        self.assertEqual(errors, [])
        self.assertTrue(any(command == 'TX_PULSES' for command, _, _ in fake.calls))
