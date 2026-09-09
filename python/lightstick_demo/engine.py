"""Shared lifecycle and transactional execution for GUI, CLI and connectors."""
import threading
from contextlib import nullcontext
from .controller import Controller, TxOutcomeUncertainError
from .discovery import AutoDiscovery, validate_bridge_info
from .model import LogicalState, LogicalUpdate, RadioSettings
from .protocols.registry import ProtocolRegistry
from .scheduler import Scheduler
from .state import StateStore
from .transports import TransportError

class Engine:
    def __init__(self, store=None, discovery=None, registry=None, protocol_id=None):
        self.store = store or StateStore()
        self.discovery = discovery or AutoDiscovery()
        self.registry = registry or ProtocolRegistry()
        state = self.store.load_unlocked()
        self.protocol = self.registry.resolve(protocol_id or state['selected_protocol'])
        self.warning = '' if (protocol_id or state['selected_protocol']) in self.registry.plugins else 'Selected protocol unavailable; using ' + self.protocol.display_name
        if self.registry.errors:
            self.warning += " Plugin errors: " + str(self.registry.errors)
        self.transport = None
        self.connection = None
        self.radio = RadioSettings()
        self._tx_lock = threading.RLock()
        self._input_lock = threading.RLock()
        self.scheduler = Scheduler(self._execute_logical)
        self._accepting = True
        self._closed = False

    def start(self):
        with self._input_lock:
            if self._closed:
                raise RuntimeError('Engine closed')
            self._accepting = True
            self.scheduler.start()

    def connect(self, requested='auto', *, lock_held=False):
        with self._input_lock:
            self.scheduler.stop()
            return self._connect(requested, lock_held=lock_held)

    def _connect(self, requested='auto', *, lock_held=False):
        with self._tx_lock:
            if self._closed:
                raise RuntimeError('Engine closed')
            if self.transport is not None:
                return self.connection
            with nullcontext(self.store) if lock_held else self.store.locked():
                state = self.store.load_unlocked()
                found = self.discovery.connect(requested, cached=state.get('connection'))
                try:
                    state['connection'] = found.cache_entry
                    self.store.save_unlocked(state)
                except Exception:
                    found.transport.disconnect()
                    raise
                self.scheduler.reset(recover=True)
                self._accepting = True
                self.transport, self.connection = found.transport, found
                return found

    def attach(self, transport):
        with self._input_lock:
            self.scheduler.stop()
            return self._attach(transport)

    def _attach(self, transport):
        with self._tx_lock:
            if self._closed:
                raise RuntimeError('Engine closed')
            validate_bridge_info(transport.request('GET_INFO', {}))
            self.scheduler.reset(recover=True)
            old = self.transport
            self.transport = transport
            if old is not None and old is not transport:
                old.disconnect()

    def select_protocol(self, protocol_id):
        with self._input_lock:
            plugin = self.registry.plugins[protocol_id]
            self.scheduler.reset()
            with self._tx_lock, self.store.locked():
                state = self.store.load_unlocked()
                state['selected_protocol'] = protocol_id
                self.store.save_unlocked(state)
                self.protocol = plugin

    def _validate(self, updates):
        for update in updates:
            self.protocol.capabilities.validate(update)

    def submit_update(self, updates):
        updates = (updates,) if isinstance(updates, LogicalUpdate) else tuple(updates)
        with self._input_lock:
            if not self._accepting:
                raise RuntimeError('Engine stopped')
            updates = tuple(LogicalUpdate(tuple(z for z in u.zones if z in self.protocol.capabilities.zones), u.rgb, u.effect, u.palette)
                            if u.rgb == (0,0,0) and u.effect in ('solid','off') else u
                            for u in updates
                            if not (u.rgb == (0,0,0) and u.effect in ('solid','off') and not set(u.zones).intersection(self.protocol.capabilities.zones)))
            try:
                self._validate(updates)
            except ValueError as exc:
                self.scheduler.unsupported(exc)
                raise
            self.scheduler.start()
            return self.scheduler.submit(updates)

    def preview(self, updates, radio=None):
        updates = (updates,) if isinstance(updates, LogicalUpdate) else tuple(updates)
        self._validate(updates)
        state = self.store.load_unlocked()
        plugin_state = state['protocol_states'].get(self.protocol.id, self.protocol.initial_state())
        return self.protocol.build_plan(LogicalState().apply(updates), plugin_state, radio or self.radio)

    def execute_update(self, updates, radio=None, *, lock_held=False):
        updates = (updates,) if isinstance(updates, LogicalUpdate) else tuple(updates)
        with self._input_lock:
            if self.scheduler.status()['running'] or self.scheduler.status()['pending']:
                raise RuntimeError('Scheduled TX active; use submit_update')
            self.scheduler.reset()
            self._validate(updates)
            return self._execute_logical(LogicalState().apply(updates), radio, lock_held=lock_held)

    def _execute_logical(self, logical, radio=None, *, lock_held=False):
        with self._tx_lock:
            if self.transport is None:
                raise TransportError('Bridge is not connected')
            with nullcontext(self.store) if lock_held else self.store.locked():
                state = self.store.load_unlocked()
                plugin_state = state['protocol_states'].get(self.protocol.id, self.protocol.initial_state())
                plan = self.protocol.build_plan(logical, plugin_state, radio or self.radio)
                try:
                    results = Controller(self.transport).execute_commands(plan.commands)
                except TxOutcomeUncertainError as exc:
                    self.scheduler.suspend(exc)
                    raise
                state['protocol_states'][self.protocol.id] = plan.next_state
                state['selected_protocol'] = self.protocol.id
                # Compatibility state exports are owned by plugins.
                export = getattr(self.protocol, 'export_legacy', None)
                if export:
                    state.update(export(plan.next_state))
                try:
                    self.store.save_unlocked(state)
                except Exception as exc:
                    error = TxOutcomeUncertainError('TX completed but state commit failed; reconnect after repairing state: ' + str(exc))
                    self.scheduler.suspend(error)
                    raise error from exc
                return results

    def execute_commands(self, commands):
        with self._input_lock:
            self.scheduler.reset()
            return self._execute_commands(commands)

    def _execute_commands(self, commands):
        with self._tx_lock, self.store.locked():
            if self.transport is None:
                raise TransportError('Bridge is not connected')
            return Controller(self.transport).execute_commands(commands)

    def status(self):
        return {'connected': self.transport is not None, 'protocol': self.protocol.id, 'zones': self.protocol.capabilities.zones,
                'warning': self.warning, 'tx': self.scheduler.status()}

    def disconnect(self):
        with self._input_lock:
            self._accepting = False
        self.scheduler.stop()
        with self._tx_lock:
            transport, self.transport = self.transport, None
            self.connection = None
            if transport is not None:
                transport.disconnect()

    def stop(self):
        with self._input_lock:
            self._closed = True
        self.disconnect()
