import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from lightstick_demo.engine import Engine
from lightstick_demo.server.manager import ServerManager
from lightstick_demo.server.lumaflow import parse_packet as parse_luma
from lightstick_demo.server.cuepilot import parse_packet as parse_osc
from lightstick_demo.state import StateStore
from test_cli_control import FakeTransport

def tlv(payload, command=0xD8):
    body = bytes([len(payload)+1, command]) + payload
    return b'\xeb\x90' + body + bytes([sum(body)&255, 0xED])

def osc_string(value):
    data = value.encode() + b'\0'
    return data + b'\0' * (-len(data)%4)

def osc(zone='A', value=15):
    return osc_string('/lightstick/zone') + osc_string(',siiis') + osc_string(zone) + struct.pack('>iii',value,0,0) + osc_string('solid')

def wait_for(predicate, timeout=3):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate(): return True
        time.sleep(.01)
    return False

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(StateStore(Path(self.temp.name)), protocol_id='protocol_d8')
        self.engine.transport = FakeTransport('test')
        self.manager = ServerManager(self.engine)
    def tearDown(self):
        self.manager.stop(); self.engine.stop(); self.temp.cleanup()
    def send(self, packet):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, self.manager.status()['address'])
    def test_luma_parser_and_malformed(self):
        updates = parse_luma(tlv(bytes([0x26,0xCF])+bytes(18)))
        self.assertEqual(updates[0].rgb, (6,12,15)); self.assertEqual(updates[0].effect,'medium')
        for bad in (b'', b'bad', tlv(bytes(19)), tlv(bytes(20))[:-1], tlv(bytes([0xF0])+bytes(19))):
            with self.assertRaises(ValueError): parse_luma(bad)
    def test_osc_parser_and_malformed(self):
        self.assertEqual(parse_osc(osc())[0].zones, ('A',))
        for bad in (b'bad', osc()[:-1], osc()+b'\0', osc(value=16)):
            with self.assertRaises(ValueError): parse_osc(bad)
    def test_loopback_switch_releases_port_and_dedupes(self):
        self.manager.switch('lumaflow', {'host':'127.0.0.1','port':0})
        address = self.manager.status()['address']
        packet = tlv(bytes([15,0])+bytes(18))
        self.send(packet)
        self.assertTrue(wait_for(lambda:self.engine.status()['tx']['transmitted']==1))
        self.send(packet)
        self.assertTrue(wait_for(lambda:self.manager.status()['deduplicated']==1))
        self.manager.switch('cuepilot', {'host':address[0],'port':address[1]})
        self.send(osc('B',7))
        self.assertTrue(wait_for(lambda:self.engine.status()['tx']['transmitted']==2))
        self.manager.stop()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock: sock.bind(address)
        self.assertTrue(self.engine.status()['connected'])
    def test_active_j_rejected_explicitly(self):
        self.manager.switch('lumaflow', {'host':'127.0.0.1','port':0})
        self.send(tlv(bytes(18)+bytes([15,0])))
        self.assertTrue(wait_for(lambda:self.engine.status()['tx']['unsupported']==1))
        self.assertIn('J', self.engine.status()['tx']['error'])
        self.assertEqual(self.engine.status()['tx']['transmitted'], 0)
        self.assertTrue(wait_for(lambda:self.manager.status()['rejected']==1))
        self.assertEqual(self.manager.status()['malformed'], 0)
    def test_port_conflict_inactive_and_manual_survives(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1',0))
            with self.assertRaises(OSError): self.manager.switch('cuepilot', {'host':'127.0.0.1','port':sock.getsockname()[1]})
        self.assertFalse(self.manager.status()['active'])
        from lightstick_demo.model import LogicalUpdate
        self.engine.execute_update(LogicalUpdate(('A',),(1,2,3)))
    def test_auth_no_tx(self):
        payload = struct.pack('<II',1,2)+b'\x01x'
        self.assertEqual(parse_luma(tlv(payload,0xE0)), ())

class AdditionalServerTests(unittest.TestCase):
    setUp = ServerTests.setUp
    tearDown = ServerTests.tearDown
    send = ServerTests.send
    def test_stream_survives_disconnect_and_uncertain_tx_then_attach(self):
        from lightstick_demo.transports import TransportTimeoutError
        for connector, packet in [('cuepilot', osc('A', 3)),
                                  ('lumaflow', tlv(bytes([3, 0])+bytes(18)))]:
            for failure in ('disconnect', 'uncertain'):
                with self.subTest(connector=connector, failure=failure):
                    self.engine.attach(FakeTransport('before'))
                    self.manager.switch(connector, {'host':'127.0.0.1','port':0})
                    address = self.manager.status()['address']
                    if failure == 'disconnect':
                        self.engine.disconnect()
                    else:
                        self.engine.transport.responses = [TransportTimeoutError('lost TX ACK')]
                        self.send(packet)
                        self.assertTrue(wait_for(lambda:self.engine.status()['tx']['suspended']))
                        self.assertTrue(self.engine.scheduler.wait_idle())
                    for count in range(1, 4):
                        self.send(packet)
                        self.assertTrue(wait_for(lambda:self.manager.status().get('rejected',0)==count))
                    self.assertTrue(self.manager.status()['active'])
                    self.assertEqual(self.manager.status()['malformed'], 0)
                    self.assertEqual(self.engine.status()['tx']['pending'], 0)
                    recovered = FakeTransport('recovered')
                    self.engine.attach(recovered)
                    self.assertEqual(recovered.calls[0][0], 'GET_INFO')
                    self.assertEqual(len(recovered.calls), 1)  # rejected input is never replayed
                    before = self.engine.status()['tx']['transmitted']
                    self.send(packet)
                    self.assertTrue(wait_for(lambda:self.engine.status()['tx']['transmitted']==before+1))
                    self.assertEqual(self.manager.status()['address'], address)
                    self.assertEqual(self.manager.status()['error'], '')
                    self.manager.stop()
    def test_malformed_then_valid_keeps_listener_alive(self):
        self.manager.switch('cuepilot', {'host':'127.0.0.1','port':0})
        self.send(b'bad')
        self.assertTrue(wait_for(lambda:self.manager.status()['malformed']==1))
        self.send(osc('A',3))
        self.assertTrue(wait_for(lambda:self.engine.status()['tx']['transmitted']==1))
        self.assertTrue(self.manager.status()['active'])
    def test_loopback_short_protocol(self):
        self.engine.select_protocol('protocol_00')
        self.manager.switch('cuepilot', {'host':'127.0.0.1','port':0})
        self.send(osc('P',7))
        self.assertTrue(wait_for(lambda:self.engine.status()['tx']['transmitted']==1))
        self.assertEqual(self.engine.status()['tx']['failed'],0)
    def test_crash_isolated_and_socket_released(self):
        from lightstick_demo.server.cuepilot import CuePilot
        class Crashing(CuePilot):
            id='crash'
            def parse(self, packet): raise RuntimeError('injected parser crash')
        self.manager.registry.plugins['crash']=Crashing()
        self.manager.switch('crash', {'host':'127.0.0.1','port':0})
        address=self.manager.status()['address']; self.send(b'anything')
        self.assertTrue(wait_for(lambda:not self.manager.status()['active']))
        self.manager.stop()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock: sock.bind(address)
        self.assertTrue(self.engine.status()['connected'])
    def test_close_disallows_late_start(self):
        self.manager.close()
        with self.assertRaises(RuntimeError): self.manager.switch('cuepilot')
