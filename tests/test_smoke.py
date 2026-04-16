"""Smoke tests — verify the package imports and the DB migrates cleanly."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class TestImports(unittest.TestCase):
    def test_package_imports(self):
        import aisha  # noqa: F401
        from aisha.core import chat, config, gateway, identity, memory, store  # noqa: F401
        from aisha.forge import registry  # noqa: F401

    def test_channels_import(self):
        from aisha.channels import slack, telegram, whatsapp, whatsapp_listener  # noqa: F401


class TestStore(unittest.TestCase):
    def test_migrations_apply(self):
        from aisha.core import store, config

        with tempfile.TemporaryDirectory() as tmp:
            orig = config.DB_PATH
            config.DB_PATH = Path(tmp) / "aisha.db"
            try:
                conn = store.connect()
                row = conn.execute("PRAGMA user_version").fetchone()
                self.assertGreater(row[0], 0)
            finally:
                config.DB_PATH = orig


if __name__ == "__main__":
    unittest.main()
