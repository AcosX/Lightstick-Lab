from ..plugin_registry import Registry

class ProtocolRegistry(Registry):
    def __init__(self, package='lightstick_demo.protocols'):
        super().__init__(package, 'PROTOCOL', ('id', 'display_name', 'capabilities'),
                         ('initial_state', 'migrate_state', 'build_plan'))
