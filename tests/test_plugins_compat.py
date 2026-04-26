import importlib.util
import sys
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "blacs" / "plugins" / "__init__.py"


class FakeManager:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.discover_modules_called = False
        self.calls = []

    def discover_modules(self):
        self.discover_modules_called = True
        return {"example": object()}

    def get_event_handlers(self, name):
        self.calls.append(("get_event_handlers", name))
        return [f"event:{name}"]


def load_plugins_module():
    module_name = "test_blacs_plugins_module"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)

    fake_labscript_utils = types.ModuleType("labscript_utils")
    fake_labconfig = types.ModuleType("labscript_utils.labconfig")
    fake_labconfig.LabConfig = lambda: object()
    fake_plugins = types.ModuleType("labscript_utils.plugins")
    fake_plugins.DEFAULT_PRIORITY = 10
    fake_plugins.BasePlugin = object
    fake_plugins.Callback = object
    fake_plugins.PluginManager = FakeManager
    fake_plugins.callback = lambda *args, **kwargs: ("callback", args, kwargs)

    fake_blacs = types.ModuleType("blacs")
    fake_blacs.BLACS_DIR = "/tmp/blacs"

    saved_modules = {}
    for name, fake_module in {
        "labscript_utils": fake_labscript_utils,
        "labscript_utils.labconfig": fake_labconfig,
        "labscript_utils.plugins": fake_plugins,
        "blacs": fake_blacs,
    }.items():
        saved_modules[name] = sys.modules.get(name)
        sys.modules[name] = fake_module

    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for name, saved_module in saved_modules.items():
            if saved_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_module


class PluginsCompatTests(unittest.TestCase):
    def test_get_callbacks_uses_manager_modern_interface(self):
        module = load_plugins_module()

        handlers = module.get_callbacks("shot_complete")

        self.assertEqual(handlers, ["event:shot_complete"])
        self.assertEqual(module.manager.calls, [("get_event_handlers", "shot_complete")])
        self.assertEqual(module.modules.keys(), {"example"})
        self.assertTrue(module.manager.discover_modules_called)


if __name__ == "__main__":
    unittest.main()
