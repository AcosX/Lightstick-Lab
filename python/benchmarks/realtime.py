"""UDP-to-fake-RF benchmark. No hardware is accessed or RF emitted."""
import argparse
import json
import socket
import struct
import tempfile
import time
from pathlib import Path
from lightstick_demo.engine import Engine
from lightstick_demo.server.manager import ServerManager
from lightstick_demo.state import StateStore

class FakeRadio:
    def __init__(self, duration): self.duration=duration; self.calls=0
    def request(self, command, args=None, timeout=0):
        if command == 'TX_PULSES': self.calls+=1; return {'request_id':str(self.calls)}
        if command == 'GET_TX_RESULT': time.sleep(self.duration); return {'status':'success'}
        return {'status':'success'}
    def disconnect(self): pass

def string(value):
    data=value.encode()+b'\0'; return data+b'\0'*(-len(data)%4)

def packet(connector, value):
    if connector == 'cuepilot':
        return string('/lightstick/zone')+string(',siiis')+string('A')+struct.pack('>iii',value,0,0)+string('solid')
    body=bytes([21,0xD8,value,0])+bytes(18)
    return b'\xeb\x90'+body+bytes([sum(body)&255,0xED])

def run(seconds=2, tx_seconds=.78575):
    rows=[]
    for connector in ('lumaflow','cuepilot'):
        for hz in (1,2,5,6,10):
            for repeat in (False,True):
                with tempfile.TemporaryDirectory() as directory:
                    engine=Engine(StateStore(Path(directory)),protocol_id='protocol_d8')
                    engine.transport=FakeRadio(tx_seconds)
                    manager=ServerManager(engine)
                    try:
                        manager.switch(connector,{'host':'127.0.0.1','port':0})
                        start=time.monotonic(); count=max(2,int(seconds*hz)); maximum=0
                        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                            for index in range(count):
                                time.sleep(max(0,start+index/hz-time.monotonic()))
                                sock.sendto(packet(connector,1 if repeat else index%15+1),manager.status()['address'])
                                maximum=max(maximum,engine.status()['tx']['pending'])
                        deadline=time.monotonic()+2
                        while manager.status()['received']<count and time.monotonic()<deadline: time.sleep(.005)
                        assert engine.scheduler.wait_idle(5)
                        stats=engine.status()['tx']
                        assert stats['failed']==0 and maximum<=1
                        if repeat: assert stats['transmitted']==1
                        rows.append(dict(connector=connector,hz=hz,repeated=repeat,packets=count,max_pending=maximum,**stats))
                    finally: manager.stop(); engine.stop()
    return rows

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--seconds',type=float,default=2)
    parser.add_argument('--output',default='benchmark.json'); args=parser.parse_args()
    rows=run(args.seconds); Path(args.output).write_text(json.dumps(rows,indent=2)+'\n')
    print(f'{len(rows)} scenarios passed; bounded pending, no failed TX')
