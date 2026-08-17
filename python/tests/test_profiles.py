# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
import json
import tempfile
import unittest
from pathlib import Path

from lightstick_demo.profiles import ProfileStore


class ProfileStoreTests(unittest.TestCase):
    def test_crud_use_and_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = ProfileStore(path)
            profile = {"transport": {"kind": "USB Serial"}, "wifi": {"ssid": "lab"}}
            store.set("  Lab  ", profile)
            self.assertEqual(store.names(), ["Lab"])
            self.assertEqual(store.get("Lab"), profile)
            self.assertEqual(store.use("Lab"), profile)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["Lab"], profile)
            store.delete("Lab")
            self.assertEqual(store.names(), [])

    def test_invalid_store_root_is_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(ProfileStore(path).names(), [])


if __name__ == "__main__":
    unittest.main()
