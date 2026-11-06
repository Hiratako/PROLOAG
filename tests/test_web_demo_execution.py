from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest

from web_demo.catalog import _execute_view


class PrivateViewExecutionTests(unittest.TestCase):
    def test_eight_calls_keep_private_globals_and_serialize_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "view.py"
            path.write_text(
                "import sys\n"
                "import threading\n"
                "import time\n"
                "payload = [threading.get_ident()]\n"
                "observed_argv = list(sys.argv)\n"
                "observed_module = sys.modules.get('__proloag_exhibition__')\n"
                "with observed_module.measurement_lock:\n"
                "    observed_module.active += 1\n"
                "    observed_module.maximum_active = max(observed_module.maximum_active, observed_module.active)\n"
                "time.sleep(0.03)\n"
                "with observed_module.measurement_lock:\n"
                "    observed_module.active -= 1\n"
                "namespace_identity = id(globals())\n",
                encoding="utf-8",
            )
            module_key = "__proloag_exhibition__"
            missing = object()
            previous = sys.modules.get(module_key, missing)
            sentinel = types.ModuleType(module_key)
            sentinel.measurement_lock = threading.Lock()
            sentinel.active = 0
            sentinel.maximum_active = 0
            sys.modules[module_key] = sentinel
            try:
                argv = list(sys.argv)
                modules = dict(sys.modules)
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(executor.map(_execute_view, [path] * 8))
                self.assertEqual(len({id(result) for result in results}), 8)
                self.assertEqual(len({id(result["payload"]) for result in results}), 8)
                self.assertEqual(len({result["namespace_identity"] for result in results}), 8)
                self.assertTrue(all(result["observed_argv"] == argv for result in results))
                self.assertTrue(all(result["observed_module"] is sentinel for result in results))
                results[0]["payload"].append("visitor change")
                self.assertTrue(all(len(result["payload"]) == 1 for result in results[1:]))
                self.assertEqual(sys.argv, argv)
                self.assertEqual(sys.modules, modules)
                self.assertIs(sys.modules[module_key], sentinel)
                self.assertEqual(sentinel.maximum_active, 1)
                self.assertEqual(sentinel.active, 0)
            finally:
                if previous is missing:
                    sys.modules.pop(module_key, None)
                else:
                    sys.modules[module_key] = previous

    def test_source_metadata_is_local_and_no_bytecode_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "view.py"
            path.write_text("file_value=__file__\npackage_value=__package__\n", encoding="utf-8")
            result = _execute_view(path)
            self.assertEqual(result["file_value"], str(path.resolve()))
            self.assertIsNone(result["package_value"])
            self.assertFalse((path.parent / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
