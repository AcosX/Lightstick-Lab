import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from lightstick_demo.protocols.registry import ProtocolRegistry
from lightstick_demo.server.registry import ServerRegistry

class RegistryTests(unittest.TestCase):
    def test_drop_in_module_is_discovered_and_bad_modules_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)/'fixture_plugins'; package.mkdir(); (package/'__init__.py').write_text('')
            (package/'aaa.py').write_text("from lightstick_demo.protocols.protocol_00 import Protocol\nclass Demo(Protocol):\n    id='test_only'\n    display_name='Third plugin'\nPROTOCOL=Demo()\n")
            (package/'duplicate.py').write_text('from .aaa import PROTOCOL\n')
            (package/'broken.py').write_text("raise RuntimeError('broken import')\n")
            (package/'incomplete.py').write_text('PROTOCOL=object()\n')
            (package/'_ignored.py').write_text("raise RuntimeError('must not load')\n")
            sys.path.insert(0,tmp)
            try:
                registry=ProtocolRegistry('fixture_plugins')
                self.assertEqual(list(registry.plugins),['test_only'])
                self.assertEqual(set(registry.errors),{'duplicate','broken','incomplete'})
                self.assertEqual(registry.resolve('missing').display_name,'Third plugin')
            finally:
                sys.path.remove(tmp)
                for key in list(sys.modules):
                    if key.startswith('fixture_plugins'): del sys.modules[key]
    def test_both_connectors_discovered(self):
        self.assertEqual(set(ServerRegistry().plugins), {'cuepilot','lumaflow'})
