"""Palette and masked-zone short commands."""
from ..model import Capabilities, TransmissionPlan
from . import _common as air

PALETTE_HEX = ('FF0000','00B51A','1878FF','FF007C','FFFFFF','FFD400','66CCFF','00D878',
               '8A4DFF','FF6A00','FF8AE0','1B90FF','FFF29A','007B66','FF5C5C','F8FAFF')
PALETTE = tuple((i, air.COLOR_NAMES[i], '#' + color) for i, color in enumerate(PALETTE_HEX))

class Protocol:
    id = 'protocol_00'
    display_name = '00 短命令'
    capabilities = Capabilities(tuple('ABCDEFGHIJKLMNOP'),
        ('off','solid','slow','medium','fast','fade_in','fade_out','hold'), 'palette', PALETTE, True, True)

    def initial_state(self):
        return {}

    def migrate_state(self, state, legacy=None):
        return dict(state)

    def build_plan(self, logical, state, radio):
        groups = {}
        for zone, value in logical.values:
            effect = {'off':0,'solid':1,'slow':2,'medium':3,'fast':4,'fade_in':5,'fade_out':6,'hold':11}[value.effect]
            color = value.palette
            if color is None:
                color = min(range(16), key=lambda i: sum((value.rgb[c]*17 - int(PALETTE_HEX[i][c*2:c*2+2],16))**2 for c in range(3)))
            if color not in range(16) and color != 0xAA:
                raise ValueError('Unsupported palette color')
            groups.setdefault((effect, color), []).append(zone)
        frames = tuple(air.build_partition_frame(*air.zone_mask(zones), state=effect, color=color)
                       for (effect,color),zones in groups.items())
        commands = tuple(('TX_PULSES', air.tx_pulses_args(air.encode_air_pulses(frame),
                         radio.frequency_hz, radio.power_dbm, radio.repeat, radio.gap_us)) for frame in frames)
        return TransmissionPlan(commands, dict(state), frames)

PROTOCOL = Protocol()
