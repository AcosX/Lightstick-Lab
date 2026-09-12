"""Nine-zone RGB protocol; reliable six-phase waveform is unchanged."""
from ..model import Capabilities, TransmissionPlan
from . import _common as air

class Protocol:
    id = 'protocol_d8'
    display_name = 'D8 RGB'
    capabilities = Capabilities(air.D8_ZONES)

    def initial_state(self):
        return {'slots': [air.d8_word(15, 0, 0)] + [0] * 8}

    def migrate_state(self, state, legacy=None):
        slots = state.get('slots', (legacy or {}).get('d8_slots', self.initial_state()['slots']))
        air._validate_d8_slots(slots)
        return {**state, 'slots': list(slots)}

    def export_legacy(self, state):
        return {'d8_slots': list(state['slots'])}

    def build_plan(self, logical, state, radio):
        slots = list(self.migrate_state(state)['slots'])
        for zone, value in logical.values:
            function = {'solid': 0, 'slow': 1, 'medium': 2, 'fast': 3, 'off': 0}[value.effect]
            rgb = (0, 0, 0) if value.effect == 'off' else value.rgb
            slots[self.capabilities.zones.index(zone)] = air.d8_word(*rgb, function)
        pulses = air.build_d8_transaction(slots)
        args = air.tx_pulses_args(pulses, radio.frequency_hz, radio.power_dbm, 1, radio.gap_us)
        return TransmissionPlan((('TX_PULSES', args),), {'slots': slots}, air.build_d8_frames_from_slots(slots))

PROTOCOL = Protocol()
