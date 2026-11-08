"""Test bootstrap: put the plugin's ``lib/`` and the N.E.K.O app root first on ``sys.path``.

The plugin's tests import app packages (``plugin``, ``utils``, ``config``, ``main_logic``)
that live at the N.E.K.O root and may resolve vendored deps from ``lib/``. A generic
top-level name like ``utils`` is easy to shadow by an earlier ``sys.path`` entry, so
prepend the app root (and ``lib/``) explicitly — duplicates are harmless, since import
resolution uses the first matching directory.
"""
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]     # <...>/qq_auto_reply
_LIB = _PLUGIN_ROOT / "lib"

# Scan upward for the N.E.K.O app root (a directory holding the `plugin` and `utils`
# top-level packages). Deployed layout: <root>/plugin/plugins/qq_auto_reply -> <root>.
_APP_ROOT = _PLUGIN_ROOT
for _ancestor in _PLUGIN_ROOT.parents:
    if (_ancestor / "plugin").is_dir() and (_ancestor / "utils").is_dir():
        _APP_ROOT = _ancestor
        break

sys.path.insert(0, str(_APP_ROOT))
sys.path.insert(0, str(_LIB))

# Standalone checkout beside the host: test THIS checkout, not an older installed
# plugin at <host>/plugin/plugins/qq_auto_reply.
if _APP_ROOT == _PLUGIN_ROOT:
    import importlib.util
    import os

    _host = Path(os.environ.get("NEKO_APP_ROOT", str(_PLUGIN_ROOT.parent / "N.E.K.O")))
    if (_host / "plugin").is_dir() and (_host / "utils").is_dir():
        sys.path.insert(0, str(_host))
        # Keep imports and config migrations away from the user's real data.
        _isolation_path = _host / "tests" / "real_root_isolation.py"
        if _isolation_path.is_file():
            from utils.config_manager import ConfigManager
            _isolation_spec = importlib.util.spec_from_file_location("_qq_test_root_isolation", _isolation_path)
            _isolation = importlib.util.module_from_spec(_isolation_spec)
            _isolation_spec.loader.exec_module(_isolation)
            _isolation.install(ConfigManager)
        import plugin.plugins

        _name = "plugin.plugins.qq_auto_reply"
        _spec = importlib.util.spec_from_file_location(
            _name, _PLUGIN_ROOT / "__init__.py",
            submodule_search_locations=[str(_PLUGIN_ROOT)],
        )
        _module = importlib.util.module_from_spec(_spec)
        sys.modules[_name] = _module
        _spec.loader.exec_module(_module)
        setattr(plugin.plugins, "qq_auto_reply", _module)
        # pytest names an invalid-identifier checkout (e.g. n.e.k.o_plugin_...)
        # simply "__init__" when setting up its package collector.
        if not _PLUGIN_ROOT.name.isidentifier():
            sys.modules.setdefault("__init__", _module)
