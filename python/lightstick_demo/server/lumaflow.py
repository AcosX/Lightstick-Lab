"""LumaFlow host TLV stream input (not an air-protocol encoder)."""
from ._base import UdpConnector
from ..model import LogicalUpdate


def parse_packet(packet):
    updates = []
    offset = 0
    while offset < len(packet):
        if packet[offset:offset+2] != b'\xeb\x90' or len(packet)-offset < 6:
            raise ValueError('Invalid TLV header')
        length = packet[offset+2]
        end = offset + length + 5
        if length < 1 or end > len(packet) or packet[end-1] != 0xED:
            raise ValueError('Invalid TLV length/terminator')
        if sum(packet[offset+2:end-2]) & 255 != packet[end-2]:
            raise ValueError('Invalid TLV checksum')
        command = packet[offset+3]
        payload = packet[offset+4:end-2]
        if command == 0xE0:
            if len(payload) < 10 or not 1 <= payload[8] <= 72 or len(payload) != 9+payload[8]:
                raise ValueError('Invalid AUTH shape')
            if int.from_bytes(payload[:4], 'little') > int.from_bytes(payload[4:8], 'little'):
                raise ValueError('Expired AUTH timestamps')
            # AUTH is metadata, not authentication: no license key verification is claimed.
        elif command == 0xD8:
            if len(payload) != 20:
                raise ValueError('STREAM requires 20 bytes')
            for index in range(10):
                first, second = payload[index*2:index*2+2]
                function = first >> 4
                if function > 3:
                    raise ValueError('Unsupported STREAM function')
                updates.append(LogicalUpdate((chr(65+index),), (first&15, second>>4, second&15),
                                              ('solid','slow','medium','fast')[function]))
        else:
            raise ValueError('Unsupported TLV command')
        offset = end
    if not packet:
        raise ValueError('Empty datagram')
    return tuple(updates)

class LumaFlow(UdpConnector):
    id = 'lumaflow'
    display_name = 'LumaFlow UDP'
    default_config = {'host':'0.0.0.0', 'port':32712}
    config_schema = {'host':'string', 'port':'integer'}
    parse = staticmethod(parse_packet)

CONNECTOR = LumaFlow()
