from ..plugin_registry import Registry

class ProtocolRegistry(Registry):
    def __init__(self, package='lightstick_demo.protocols'):
        super().__init__(package, 'PROTOCOL', ('id', 'display_name', 'capabilities'),
                         ('initial_state', 'migrate_state', 'build_plan'))
        from ..model import Capabilities
        for key, plugin in list(self.plugins.items()):
            try:
                if not isinstance(plugin.capabilities, Capabilities):
                    raise ValueError('capabilities must be Capabilities')
                if not plugin.capabilities.zones or not plugin.capabilities.effects:
                    raise ValueError('Protocol needs zones and effects')
                if not isinstance(plugin.initial_state(), dict):
                    raise ValueError('initial_state must return a dict')
            except Exception as exc:
                self.errors[key] = str(exc)
                del self.plugins[key]
        if not self.plugins:
            raise ValueError('No usable protocols: ' + str(self.errors))
