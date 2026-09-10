"""Exercise actual Tk command bindings against a fake bridge, without RF."""
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from lightstick_demo.ui import LightstickApp
from lightstick_demo.state import StateStore
from lightstick_demo.transports import BleTransport, TransportStatus
from test_cli_control import FakeTransport
from test_recording import RecordingTransport

class FakeBridge(FakeTransport):
    def __init__(self, endpoint='fake', *args):
        super().__init__(endpoint)
        self.profiles = {'saved': {'name':'saved','kind':'pulse','durations_us':[250,500]}}
        self.recording = RecordingTransport()
    def request(self, command, args=None, timeout=8):
        args = args or {}
        if command in ('TX_PULSES','TX_PACKET','GET_TX_RESULT','ABORT','GET_INFO'):
            if command == 'TX_PACKET':
                self.calls.append((command,args,timeout)); return {'status':'success'}
            return super().request(command,args,timeout)
        self.calls.append((command,args,timeout))
        if command == 'GET_STATUS': return {'status':'ready'}
        if command == 'LIST_PROFILES': return {'profiles':list(self.profiles.values())}
        if command == 'GET_PROFILE': return self.profiles[args['name']]
        if command == 'SET_PROFILE': self.profiles[args['profile']['name']]=args['profile']; return {'status':'success'}
        if command == 'DELETE_PROFILE': self.profiles.pop(args['name']); return {'status':'success'}
        if command == 'GET_NETWORK_STATUS': return {'connected':True,'ip':'192.0.2.1'}
        if command in ('SET_WIFI_CONFIG','CONNECT_WIFI'): return {'status':'success'}
        if 'CLIENT_RECORDING' in command: return self.recording.request(command,args,timeout)
        raise AssertionError(command)

class FakeBleBridge(FakeBridge, BleTransport):
    pass

class InteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        try: cls.app = LightstickApp(StateStore(Path(cls.temp.name)))
        except tk.TclError as exc:
            cls.temp.cleanup(); raise unittest.SkipTest(f'Tk display unavailable: {exc}')
        cls.app.update()
    @classmethod
    def tearDownClass(cls):
        cls.app._close(); cls.temp.cleanup()
    def setUp(self):
        self.errors=[]
        self.app._show_error=lambda *args:self.errors.append(args)
        self.app.report_callback_exception=lambda *args:self.errors.append(args)
        self.fake=FakeBridge()
        self.app.transport=self.app.engine.transport=self.fake
        self.app.transport_status=TransportStatus('test',True,'fake')
        self.app.engine.start()
        self.app.air_family_var.set('00 短命令')
        self.app.rgb_hex_var.set('#FF0000')
        self.app.color_var.set(0)
        self.app.command_state_var.set('常亮')
        for zone,var in self.app.zone_vars.items():var.set(zone=='A')
        self.app.air_repeat_var.set('1')
        self.app.air_power_var.set(-20)
        self.app.update()
    def tearDown(self):
        self.drain()
        for child in self.app.winfo_children():
            if isinstance(child,tk.Toplevel):child.destroy()
        self.assertEqual(self.errors,[])
    def drain(self):
        deadline=time.monotonic()+3
        while self.app._busy_tasks and time.monotonic()<deadline:
            self.app.update(); time.sleep(.005)
        self.assertFalse(self.app._busy_tasks)
        self.app.update()
    def widgets(self,parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)
    def button(self,parent,text):
        return next(w for w in self.widgets(parent) if w.winfo_class()=='TButton' and w.cget('text')==text)
    def tx_calls(self):return [(c,a) for c,a,_ in self.fake.calls if c.startswith('TX_')]
    def test_timer_count_remains_bounded_while_idle(self):
        counts=[]; end=time.monotonic()+1.2
        while time.monotonic()<end:
            self.app.update(); counts.append(len(self.app.tk.call('after','info'))); time.sleep(.01)
        self.assertLessEqual(max(counts),2)
    def test_repeated_manual_click_and_changed_radio_each_transmit(self):
        for protocol in self.app._protocol_names:
            self.app.air_family_var.set(protocol)
            before=len(self.tx_calls())
            for power in (-20,-20,-10):
                self.app.air_power_var.set(power)
                self.app.function_buttons['常亮'].invoke(); self.drain()
            self.assertEqual(len(self.tx_calls())-before,3)
            self.assertEqual(self.tx_calls()[-1][1]['power_dbm'],-10)
    def test_hidden_bad_rgb_does_not_break_short_commands_or_blackout(self):
        self.app.rgb_hex_var.set('invalid')
        for name in ('保持','Fade in','Fade out'):
            self.app.function_buttons[name].invoke(); self.drain()
        self.app.air_family_var.set('D8 RGB')
        self.app.rgb_hex_var.set('invalid')
        self.app.function_buttons['熄灭'].invoke(); self.drain()
        self.assertEqual(len(self.tx_calls()),4)
    def test_legacy_extra_buttons_and_dialog_send(self):
        for name in ('脉冲','解锁'):
            self.button(self.app.lightstick_tab,name).invoke(); self.drain()
        self.button(self.app.lightstick_tab,'所有分区变色').invoke(); self.app.update()
        dialog=next(w for w in self.app.winfo_children() if isinstance(w,tk.Toplevel))
        self.button(dialog,'发射').invoke(); self.drain()
        self.assertEqual(len(self.tx_calls()),3)
        self.assertIsNone(self.app.grab_current())
    def test_profile_crud_and_send(self):
        self.app.open_profile_dialog(); self.drain()
        self.assertEqual(self.app.profile_list.size(),1)
        self.app.profile_list.selection_set(0)
        self.button(self.app._profile_dialog,'读取').invoke(); self.drain()
        self.assertIn('saved',self.app.profile_json.get('1.0','end'))
        self.button(self.app._profile_dialog,'发射').invoke(); self.drain()
        self.button(self.app._profile_dialog,'新建').invoke()
        self.button(self.app._profile_dialog,'保存').invoke(); self.drain()
        self.assertIn('new-profile',self.fake.profiles)
        self.app.profile_list.selection_set(0)
        self.button(self.app._profile_dialog,'删除').invoke(); self.drain()
        self.assertEqual(len(self.fake.profiles),1)
    def test_raw_command_and_recording_dialog(self):
        self.app.open_raw_dialog(); self.app.update()
        self.button(self.app._raw_dialog,'发送').invoke(); self.drain()
        self.assertTrue(any(c=='GET_STATUS' for c,_,_ in self.fake.calls))
        self.app._raw_dialog.destroy()
        self.app.open_record_dialog(); self.app.update()
        path=Path(self.temp.name)/'ui-capture.lsr'
        self.app.record_path_var.set(str(path))
        self.button(self.app._record_dialog,'开始').invoke(); self.drain()
        self.assertEqual(path.stat().st_size,56)
        self.assertIsNone(self.app.recorder)
    def test_wifi_apply_and_disconnect_status(self):
        fake=FakeBleBridge(); self.app.transport=self.app.engine.transport=fake
        self.app.open_wifi_dialog(); self.app.update()
        entries=[w for w in self.widgets(self.app._wifi_dialog) if w.winfo_class()=='TEntry']
        entries[0].insert(0,'test-network'); entries[1].insert(0,'test-only-password')
        self.button(self.app._wifi_dialog,'应用').invoke(); self.drain()
        self.assertEqual(self.app.http_url_var.get(),'http://192.0.2.1')
        self.app.disconnect_transport(); self.drain()
        self.assertFalse(self.app.transport_status.connected)
        self.assertIn('未连接',self.app.header_status.cget('text'))
    def test_usb_ble_http_connection_bindings(self):
        with patch('lightstick_demo.ui.SerialTransport') as serial:
            serial.list_ports.return_value=[{'device':'/dev/ttyUSB0','description':'USB'}]
            serial.return_value=self.fake
            self.app.scan_serial_and_connect(); self.drain()
        with patch('lightstick_demo.ui.BleTransport') as ble:
            ble.scan.return_value=[{'name':'Lightstick N16R8','address':'test'}]
            ble.return_value=FakeBridge()
            self.app.scan_ble_and_connect(); self.drain()
        self.app.http_url_var.set('http://192.0.2.1')
        with patch('lightstick_demo.ui.HttpTransport',return_value=FakeBridge()):
            self.app.connect_http(); self.drain()
        self.assertIs(self.app.engine.transport,self.app.transport)
    def test_bad_result_callback_does_not_stop_other_controls(self):
        def broken(_):raise ValueError('injected result failure')
        self.app._submit('broken-result',lambda:None,'',broken); self.drain()
        self.assertEqual(len(self.errors),1); self.errors.clear()
        self.app.refresh_bridge_status(); self.drain()
        self.assertIn('已连接',self.app.esp_status_var.get())
        self.assertTrue(any(c=='GET_STATUS' for c,_,_ in self.fake.calls))
    def test_external_start_and_stop_survive_invalid_radio_input(self):
        self.app.connector_var.set('CuePilot OSC')
        self.app._configure_connector(); self.app.update()
        dialog=next(w for w in self.app.winfo_children() if isinstance(w,tk.Toplevel))
        entries=[w for w in self.widgets(dialog) if w.winfo_class()=='TEntry']
        for entry,value in zip(entries,('127.0.0.1','0')):
            entry.delete(0,'end'); entry.insert(0,value)
        self.button(dialog,'启动').invoke(); self.drain()
        self.assertTrue(self.app.server_manager.status()['active'])
        self.app.air_repeat_var.set('bad')
        self.button(self.app.external_tab,'停止').invoke(); self.drain()
        self.assertFalse(self.app.server_manager.status()['active'])
        self.app.air_repeat_var.set('1')
    def test_all_short_command_effects_and_colors_match_legacy_frames(self):
        from lightstick_demo.protocol import build_partition_frame
        legacy_states={'熄灭':0,'常亮':1,'慢闪':2,'中闪':3,'快闪':4,'Fade in':5,'Fade out':6,'保持':11}
        for label,code in legacy_states.items():
            self.app.command_state_var.set(label)
            for color in range(16):
                self.app.color_var.set(color)
                with self.subTest(effect=label,color=color):
                    self.assertEqual(self.app._current_frames(),(build_partition_frame(1,0,state=code,color=color),))
