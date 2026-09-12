"""Deterministic discovery with per-module error isolation."""
import importlib
import pkgutil

class Registry:
    def __init__(self, package, export, attributes, methods):
        self.plugins = {}
        self.errors = {}
        package = importlib.import_module(package) if isinstance(package, str) else package
        importlib.invalidate_caches()
        for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
            if info.name.startswith('_') or info.name in ('base', 'registry', 'manager'):
                continue
            try:
                plugin = getattr(importlib.import_module(package.__name__ + '.' + info.name), export)
                for name in attributes:
                    if not hasattr(plugin, name):
                        raise ValueError('Missing ' + name)
                for name in methods:
                    if not callable(getattr(plugin, name, None)):
                        raise ValueError('Missing method ' + name)
                if not isinstance(plugin.id, str) or not plugin.id or not isinstance(plugin.display_name, str):
                    raise ValueError('Invalid plugin metadata')
                if plugin.id in self.plugins:
                    raise ValueError('Duplicate plugin ID: ' + plugin.id)
                self.plugins[plugin.id] = plugin
            except Exception as exc:
                self.errors[info.name] = str(exc)
        if not self.plugins:
            raise ValueError('No usable plugins: ' + str(self.errors))

    def resolve(self, selected):
        return self.plugins.get(selected) or next(iter(self.plugins.values()))
