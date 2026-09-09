# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Cross-platform persistent CLI state with an inter-process file lock."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator



STATE_VERSION = 2
def __getattr__(name):
    if name == 'DEFAULT_D8_SLOTS':
        from .protocols.protocol_d8 import PROTOCOL
        return tuple(PROTOCOL.initial_state()['slots'])
    raise AttributeError(name)


class StateError(RuntimeError):
    """Persistent state could not be locked, loaded, or saved."""


def default_state_dir() -> Path:
    """Return a per-user state directory appropriate for the host platform."""

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LightstickLab"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "LightstickLab"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "lightstick-lab"


def default_state() -> dict[str, Any]:
    state = {
        "version": STATE_VERSION,
        "connection": None,
        "selected_protocol": "protocol_00",
        "protocol_states": {},
        "selected_connector": None,
        "connector_configs": {},

    }
    from .protocols.registry import ProtocolRegistry
    for key, plugin in ProtocolRegistry().plugins.items():
        state['protocol_states'][key] = plugin.initial_state()
        export = getattr(plugin, 'export_legacy', None)
        if export:
            state.update(export(state['protocol_states'][key]))
    return state


class ProcessFileLock:
    """Small stdlib-only advisory lock for Unix and Windows processes."""

    def __init__(self, path: Path, timeout: float = 45.0) -> None:
        self.path = path
        self.timeout = timeout
        self._file: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._file = handle
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise StateError(f"等待状态锁超时: {self.path}")
                time.sleep(0.05)

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class StateStore:
    def __init__(self, directory: Path | None = None, lock_timeout: float = 45.0) -> None:
        self.directory = Path(directory) if directory is not None else default_state_dir()
        self.path = self.directory / "state.json"
        self._lock = ProcessFileLock(self.directory / "state.lock", timeout=lock_timeout)

    @contextmanager
    def locked(self) -> Iterator["StateStore"]:
        with self._lock:
            yield self

    def load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError(f"无法读取状态: {exc}") from exc
        if not isinstance(raw, dict):
            raise StateError("状态文件必须为 JSON object")
        state = default_state()
        connection = raw.get("connection")
        if connection is not None:
            if not isinstance(connection, dict):
                raise StateError("connection 状态格式无效")
            state["connection"] = dict(connection)
        state.update(raw)
        state['version'] = STATE_VERSION
        if raw.get('version', 1) < 2:
            state['protocol_states'] = raw.get('protocol_states', {})
        self._migrate_plugins(state)
        return state

    @staticmethod
    def _migrate_plugins(state):
        from .protocols.registry import ProtocolRegistry
        if not isinstance(state['protocol_states'], dict) or not isinstance(state['connector_configs'], dict):
            raise StateError('Plugin state/configuration must be an object')
        if not isinstance(state.get('selected_protocol'), str):
            raise StateError('selected_protocol must be a string')
        if state.get('selected_connector') is not None and not isinstance(state['selected_connector'], str):
            raise StateError('selected_connector must be a string or null')
        registry = ProtocolRegistry()
        for key, plugin in registry.plugins.items():
            try:
                state['protocol_states'][key] = plugin.migrate_state(state['protocol_states'].get(key, {}), state)
                export = getattr(plugin, 'export_legacy', None)
                if export:
                    state.update(export(state['protocol_states'][key]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise StateError(f'{key}: {exc}') from exc

    def save_unlocked(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        normalized = default_state()
        normalized["connection"] = state.get("connection")
        normalized.update(state)
        normalized['version'] = STATE_VERSION
        normalized.update({key: state[key] for key in ('selected_protocol', 'protocol_states', 'selected_connector', 'connector_configs') if key in state})
        self._migrate_plugins(normalized)
        payload = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temp_path: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.directory)
            temp_path = Path(raw_path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise StateError(f"无法保存状态: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
