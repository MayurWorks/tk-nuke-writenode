"""
Tests for the per-project write-node categories added to
NukeWriteNodeHandler (__get_categories, PER_PROJECT_WRITE_CATEGORIES).

Only this one piece of handler.py is tested here, not the whole app -
handler.py imports `nuke` directly at module level, and the rest of the
app's behavior (node creation, rendering, publishing) is unrelated to
this change and out of scope. `nuke` and `sgtk` are faked just enough
for handler.py to import cleanly and for __get_categories() to run.
"""
import os
import sys
import types
import importlib

import pytest


class FakeShotgun:
    def __init__(self, project_row=None, raise_on_find=False):
        self._project_row = project_row
        self._raise_on_find = raise_on_find
        self.find_one_calls = []

    def find_one(self, entity_type, filters, fields):
        self.find_one_calls.append((entity_type, filters, fields))
        if self._raise_on_find:
            raise RuntimeError("simulated ShotGrid connectivity failure")
        return self._project_row


class FakeContext:
    def __init__(self, project):
        self.project = project


class FakeEngine:
    def __init__(self, context):
        self.context = context


class FakeApp:
    def __init__(self, shotgun, static_categories):
        self._shotgun = shotgun
        self._static_categories = static_categories

    @property
    def shotgun(self):
        return self._shotgun

    def get_setting(self, name):
        assert name == "categories"
        return self._static_categories


@pytest.fixture
def handler_module(monkeypatch):
    """
    Imports the real tk_nuke_writenode.handler module with fake `nuke`
    and `sgtk` modules injected into sys.modules so the module-level
    `import nuke` / `import sgtk` at the top of handler.py succeed, then
    returns the module plus fresh fakes for building a handler instance
    per test.
    """
    fake_nuke = types.ModuleType("nuke")
    sys.modules["nuke"] = fake_nuke

    fake_sgtk = types.ModuleType("sgtk")
    fake_platform = types.ModuleType("sgtk.platform")

    class _CurrentEngineHolder:
        engine = None

    holder = _CurrentEngineHolder()

    def current_engine():
        return holder.engine

    def current_bundle():
        return holder.app

    def get_logger(name):
        class _NullLogger:
            def warning(self, *a, **k):
                pass

            def info(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        return _NullLogger()

    fake_platform.current_engine = current_engine
    fake_platform.current_bundle = current_bundle
    fake_platform.get_logger = get_logger
    fake_sgtk.platform = fake_platform
    sys.modules["sgtk"] = fake_sgtk
    sys.modules["sgtk.platform"] = fake_platform

    # handler.py also does `from .create_dialog import WriteNodePanel`
    # at module level - stub that submodule out too since it isn't
    # under test here and may itself import Qt bindings unavailable in
    # this environment.
    pkg_name = "tk_nuke_writenode"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [
        os.path.join(os.path.dirname(__file__), "..", "python", "tk_nuke_writenode")
    ]
    sys.modules[pkg_name] = pkg

    fake_create_dialog = types.ModuleType(pkg_name + ".create_dialog")

    class _FakeWriteNodePanel:
        def __init__(self, *a, **k):
            pass

    fake_create_dialog.WriteNodePanel = _FakeWriteNodePanel
    sys.modules[pkg_name + ".create_dialog"] = fake_create_dialog

    handler_mod = importlib.import_module(pkg_name + ".handler")
    importlib.reload(handler_mod)

    yield {"handler_mod": handler_mod, "engine_holder": holder}

    for name in list(sys.modules):
        if name == "nuke" or name.startswith("tk_nuke_writenode") or name == "sgtk" or name == "sgtk.platform":
            del sys.modules[name]


def _make_handler(hm, project_row=None, raise_on_find=False, static_categories=None):
    handler_mod = hm["handler_mod"]
    if static_categories is None:
        static_categories = [
            {
                "category_name": "main",
                "write_nodes": [{"name": "exr (dwaa 16bit)", "file_type": "exr"}],
            }
        ]
    shotgun = FakeShotgun(project_row=project_row, raise_on_find=raise_on_find)
    app = FakeApp(shotgun=shotgun, static_categories=static_categories)
    context = FakeContext(project={"type": "Project", "id": 91, "name": "storm"})
    hm["engine_holder"].engine = FakeEngine(context)
    hm["engine_holder"].app = app

    handler = handler_mod.NukeWriteNodeHandler()
    return handler, shotgun, static_categories


class TestGetCategories:
    def test_unset_pipeline_falls_back_to_static_categories(self, handler_module):
        handler, shotgun, static_categories = _make_handler(
            handler_module, project_row={"sg_color_pipeline": None}
        )
        result = handler._NukeWriteNodeHandler__get_categories()
        assert result == static_categories

    def test_recognised_pipeline_returns_override(self, handler_module):
        handler, shotgun, static_categories = _make_handler(
            handler_module, project_row={"sg_color_pipeline": "rec709_sdr"}
        )
        result = handler._NukeWriteNodeHandler__get_categories()
        handler_mod = handler_module["handler_mod"]
        assert result == handler_mod.PER_PROJECT_WRITE_CATEGORIES["rec709_sdr"]
        assert result != static_categories
        # sanity: the override actually carries the expected colorspace
        assert (
            result[0]["write_nodes"][0]["settings"]["colorspace"] == "sRGB"
        )

    def test_unrecognised_pipeline_value_falls_back_to_static(self, handler_module):
        handler, shotgun, static_categories = _make_handler(
            handler_module, project_row={"sg_color_pipeline": "some_future_pipeline"}
        )
        result = handler._NukeWriteNodeHandler__get_categories()
        assert result == static_categories

    def test_live_query_failure_falls_back_to_static(self, handler_module):
        handler, shotgun, static_categories = _make_handler(
            handler_module, raise_on_find=True
        )
        result = handler._NukeWriteNodeHandler__get_categories()
        assert result == static_categories

    def test_no_project_in_context_falls_back_to_static(self, handler_module):
        handler_mod = handler_module["handler_mod"]
        shotgun = FakeShotgun()
        app = FakeApp(shotgun=shotgun, static_categories=[{"category_name": "main", "write_nodes": []}])
        context = FakeContext(project=None)
        handler_module["engine_holder"].engine = FakeEngine(context)
        handler_module["engine_holder"].app = app

        handler = handler_mod.NukeWriteNodeHandler()
        result = handler._NukeWriteNodeHandler__get_categories()
        assert result == app._static_categories
        # confirm it never even attempted the live query with no project
        assert shotgun.find_one_calls == []

    def test_all_three_presets_have_valid_shape(self, handler_module):
        handler_mod = handler_module["handler_mod"]
        for key, categories in handler_mod.PER_PROJECT_WRITE_CATEGORIES.items():
            assert isinstance(categories, list)
            for category in categories:
                assert "category_name" in category
                assert "write_nodes" in category
                for write_node in category["write_nodes"]:
                    assert "name" in write_node
                    assert "file_type" in write_node
                    assert "render_template" in write_node
                    assert "publish_template" in write_node
                    assert "settings" in write_node
                    assert "colorspace" in write_node["settings"]
