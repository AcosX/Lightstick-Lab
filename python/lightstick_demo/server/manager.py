"""Stop-before-start switching with no bridge ownership in connectors."""
import copy
import threading
from functools import wraps
from dataclasses import dataclass
from .registry import ServerRegistry

def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call

@dataclass(frozen=True)
class Context:
    submit_update: object
    status: object
    config: dict

class ServerManager:
    def __init__(self, engine, registry=None):
        self._lock = threading.RLock()
        self._closed = False
        self.engine = engine
        self.registry = registry or ServerRegistry()
        self.connector = None
        self.error = ''

    @serialized
    def switch(self, connector_id, config=None):
        if self._closed:
            raise RuntimeError('Server manager closed')
        self.stop()
        self.error = ''
        if not connector_id:
            self._persist(None, {})
            return
        connector = None
        try:
            # A fresh instance per manager; never share sockets across windows.
            connector = copy.deepcopy(self.registry.plugins[connector_id])
            settings = {**connector.default_config, **(config or {})}
            connector.start(Context(self.engine.submit_update, self.engine.status, settings))
            self._persist(connector_id, settings)
            self.connector = connector
        except Exception as exc:
            if connector is not None:
                connector.stop()
            self.error = str(exc)
            raise

    def _persist(self, selected, config):
        with self.engine.store.locked():
            state = self.engine.store.load_unlocked()
            state['selected_connector'] = selected
            if selected:
                state['connector_configs'][selected] = config
            self.engine.store.save_unlocked(state)

    @serialized
    def stop(self):
        if self.connector is not None:
            self.connector.stop()
            self.connector = None

    @serialized
    def close(self):
        self._closed = True
        self.stop()

    def status(self):
        try:
            return self.connector.status() if self.connector else {'active': False, 'error': self.error}
        except Exception as exc:
            self.error = str(exc)
            return {'active': False, 'error': self.error}
