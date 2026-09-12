"""OSC 1.0 immediate control schema with RGB444 integer arguments."""
import struct
from ._base import UdpConnector
from ..model import LogicalUpdate


def _string(packet, offset):
    end = packet.find(b'\0', offset)
    if end < 0:
        raise ValueError('Unterminated OSC string')
    padded = (end+4) & ~3
    if padded > len(packet) or any(packet[end:padded]):
        raise ValueError('Invalid OSC padding')
    try:
        return packet[offset:end].decode('utf-8'), padded
    except UnicodeDecodeError as exc:
        raise ValueError('Invalid OSC UTF-8') from exc


def parse_packet(packet, zones=tuple('ABCDEFGHIJKLMNOP'), depth=0):
    if depth > 4:
        raise ValueError('OSC bundle nesting limit')
    if packet.startswith(b'#bundle\0'):
        if len(packet) < 16 or packet[8:16] != b'\0\0\0\0\0\0\0\1':
            raise ValueError('Only immediate OSC bundles supported')
        offset = 16; updates = []
        while offset < len(packet):
            if offset+4 > len(packet):
                raise ValueError('Truncated OSC bundle')
            size = struct.unpack_from('>I', packet, offset)[0]; offset += 4
            if not size or offset+size > len(packet):
                raise ValueError('Invalid OSC element size')
            updates.extend(parse_packet(packet[offset:offset+size], zones, depth+1)); offset += size
        return tuple(updates)
    address, offset = _string(packet, 0)
    tags, offset = _string(packet, offset)
    if not tags.startswith(','):
        raise ValueError('Missing OSC type tags')
    values = []
    for tag in tags[1:]:
        if tag == 's':
            value, offset = _string(packet, offset)
        elif tag == 'i':
            if offset+4 > len(packet):
                raise ValueError('Truncated OSC integer')
            value = struct.unpack_from('>i', packet, offset)[0]; offset += 4
        else:
            raise ValueError('Unsupported OSC type')
        values.append(value)
    if offset != len(packet):
        raise ValueError('Trailing OSC bytes')
    if address == '/lightstick/blackout' and tags == ',':
        return (LogicalUpdate(tuple(zones), effect='off'),)
    if address == '/lightstick/all' and tags == ',iiis':
        return (LogicalUpdate(tuple(zones), tuple(values[:3]), values[3]),)
    if address in ('/lightstick/zone','/lightstick/state') and tags == ',siiis':
        selected = tuple(z.strip().upper() for z in values[0].split(','))
        return (LogicalUpdate(selected, tuple(values[1:4]), values[4]),)
    raise ValueError('Unknown OSC address or argument schema')

class CuePilot(UdpConnector):
    id = 'cuepilot'
    display_name = 'CuePilot OSC'
    default_config = {'host':'0.0.0.0', 'port':9000}
    config_schema = {'host':'string', 'port':'integer'}
    def parse(self, packet):
        return parse_packet(packet, self.context.status()['zones'])

CONNECTOR = CuePilot()
