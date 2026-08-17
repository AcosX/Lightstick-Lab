# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Local named profile storage for repeatable bridge sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.home() / "Library" / "Application Support" / "LightstickDemo" / "profiles.json")

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def names(self) -> list[str]:
        return sorted(self._read())

    def get(self, name: str) -> dict[str, Any] | None:
        value = self._read().get(name)
        return value if isinstance(value, dict) else None

    def set(self, name: str, profile: dict[str, Any]) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("请输入配置名称")
        if not isinstance(profile, dict):
            raise ValueError("配置必须是 JSON object")
        data = self._read()
        data[clean_name] = dict(profile)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def delete(self, name: str) -> None:
        data = self._read()
        if name in data:
            del data[name]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)

    def use(self, name: str) -> dict[str, Any]:
        """Return a named profile for immediate application by a client/UI."""

        profile = self.get(name.strip())
        if profile is None:
            raise KeyError(f"找不到配置: {name}")
        return dict(profile)
