from ..plugin_registry import Registry

class ServerRegistry(Registry):
    def __init__(self, package='lightstick_demo.server'):
        super().__init__(package, 'CONNECTOR', ('id','display_name','config_schema','default_config'),
                         ('start','stop','status'))
