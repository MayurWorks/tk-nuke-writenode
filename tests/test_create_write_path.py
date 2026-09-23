"""
Tests that __create_write() bakes the render path into the internal
Write1 node's "file" knob immediately at creation time, for both the
"main" (exr) and "review" (mov) categories - instead of leaving it
blank until the first render (the previous behavior).

Only this path covers the new __create_write/__prepare_write
interaction; __get_categories() has its own test module
(test_per_project_categories.py) and isn't re-tested here.
"""
import os
import sys
import types
import importlib

import pytest


class FakeKnob:
    def __init__(self, value=None):
        self._value = value

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value

    def setValues(self, values):
        self._values = list(values)

    def setEnabled(self, *_):
        pass

    def setVisible(self, *_):
        pass

    def setTooltip(self, *_):
        pass


class FakeNode:
    def __init__(self, node_type=""):
        self.node_type = node_type
        self._knobs = {}

    def knob(self, name):
        return self._knobs.setdefault(name, FakeKnob())

    def __getitem__(self, name):
        return self._knobs.setdefault(name, FakeKnob())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def name(self):
        return "/jobs/SlateX/STRM/STRM_E2_0010_comp_v001.nk"

    def setName(self, *_):
        pass

    def setInput(self, *_):
        pass

    def setXYpos(self, *_):
        pass


class FakeTemplate:
    def __init__(self, definition):
        self.definition = definition

    def get_fields(self, path):
        # Enough fields for apply_fields() below to fill every {token}
        # used across both the exr and mov templates under test.
        return {
            "Sequence": "EP_2",
            "Shot": "STRM_E2_0010",
            "Step": "Comp",
            "version": "001",
            "name": "main",
        }

    def apply_fields(self, fields):
        result = self.definition
        for key, value in fields.items():
            result = result.replace("{%s}" % key, str(value))
        return result


class FakeApp:
    def __init__(self, settings):
        self._settings = settings
        self.shotgun = None
        self._templates = {
            "template_script_work": FakeTemplate("/jobs/SlateX/STRM/{Shot}.nk"),
            "nuke_shot_render": FakeTemplate(
                "/jobs/SlateX/STRM/renders/{output}/{Shot}_v{version}.{SEQ}.exr"
            ),
            "nuke_shot_render_movie": FakeTemplate(
                "/jobs/SlateX/STRM/previews/{Shot}_{name}_v{version}.mov"
            ),
        }

    def get_setting(self, name):
        return self._settings[name]

    def get_template(self, name):
        return self._templates[name]

    def get_template_by_name(self, name):
        return self._templates[name]


@pytest.fixture
def handler_module(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)

    fake_nuke = types.ModuleType("nuke")
    fake_nuke.root = lambda: FakeNode()
    fake_nuke.createNode = lambda node_type, *a, **k: FakeNode(node_type)

    created = {}

    def to_node(name):
        return created.get(name)

    fake_nuke.toNode = to_node
    fake_nuke.String_Knob = lambda *a, **k: FakeKnob()
    fake_nuke.Text_Knob = lambda *a, **k: FakeKnob()
    fake_nuke.Enumeration_Knob = lambda *a, **k: FakeKnob()

    sys.modules["nuke"] = fake_nuke

    fake_sgtk = types.ModuleType("sgtk")
    fake_platform = types.ModuleType("sgtk.platform")

    class _Holder:
        engine = types.SimpleNamespace(context=None)
        app = None

    holder = _Holder()
    fake_platform.current_engine = lambda: holder.engine
    fake_platform.current_bundle = lambda: holder.app
    fake_platform.get_logger = lambda name: types.SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    fake_sgtk.platform = fake_platform
    sys.modules["sgtk"] = fake_sgtk
    sys.modules["sgtk.platform"] = fake_platform

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

    yield {"handler_mod": handler_mod, "engine_holder": holder, "created": created,
           "fake_nuke": fake_nuke}

    for name in list(sys.modules):
        if name == "nuke" or name.startswith("tk_nuke_writenode") or name in (
            "sgtk",
            "sgtk.platform",
        ):
            del sys.modules[name]


CATEGORIES_SETTING = [
    {
        "category_name": "main",
        "write_nodes": [
            {
                "name": "exr (dwaa 16bit)",
                "file_type": "exr",
                "render_template": "nuke_shot_render",
                "publish_template": "nuke_shot_render",
                "tile_color": 111,
                "settings": {"colorspace": "ACES - ACEScg", "datatype": "16 bit half"},
            }
        ],
    },
    {
        "category_name": "review",
        "write_nodes": [
            {
                "name": "mov (h264)",
                "file_type": "mov",
                "render_template": "nuke_shot_render_movie",
                "publish_template": "nuke_shot_render_movie",
                "tile_color": 222,
                "settings": {"colorspace": "Output - Rec.709", "mov64_codec": "H.264"},
            }
        ],
    },
]


def _make_handler(hm):
    handler_mod = hm["handler_mod"]
    app = FakeApp({"categories": CATEGORIES_SETTING})
    hm["engine_holder"].app = app
    hm["engine_holder"].engine.context = types.SimpleNamespace(project=None)
    handler = handler_mod.NukeWriteNodeHandler()
    return handler


def _create(hm, handler, category, output_name, data_type):
    """
    Mirrors what create_writenode() does after the panel closes:
    build write_node_settings, create the sgWrite group, register its
    internal Write1 node in the fake nuke.toNode() table, then call
    the private __create_write the same way create_writenode() does.
    """
    write_node_settings = handler._NukeWriteNodeHandler__get_write_node_options()

    fake_nuke = hm["fake_nuke"]
    original_create_node = fake_nuke.createNode

    def create_node(node_type, *a, **k):
        node = original_create_node(node_type, *a, **k)
        if node_type == "sgWrite":
            hm["created"]["Write1"] = FakeNode("Write")
        return node

    fake_nuke.createNode = create_node

    return handler._NukeWriteNodeHandler__create_write(
        write_node_settings, category, output_name, data_type
    )


class TestCreateWriteBakesPathImmediately:
    def test_main_exr_path_set_at_creation(self, handler_module):
        handler = _make_handler(handler_module)
        _create(handler_module, handler, "main", "beauty", "exr (dwaa 16bit)")

        write1 = handler_module["created"]["Write1"]
        assert write1["file"].value() == (
            "/jobs/SlateX/STRM/renders/beauty/STRM_E2_0010_v001.FORMAT: %d.exr"
        )
        assert write1["file_type"].value() == "exr"

    def test_review_mov_path_set_at_creation(self, handler_module):
        handler = _make_handler(handler_module)
        _create(handler_module, handler, "review", "dailies", "mov (h264)")

        write1 = handler_module["created"]["Write1"]
        assert write1["file"].value() == (
            "/jobs/SlateX/STRM/previews/STRM_E2_0010_main_v001.mov"
        )
        assert write1["file_type"].value() == "mov"
        assert write1["mov64_codec"].value() == "H.264"

    def test_switching_category_would_change_both_type_and_path(self, handler_module):
        # Sanity check that main and review really do resolve to
        # different file types and different render locations - this
        # is the behavior knob_changed() now relies on when dataType
        # changes on an existing node.
        handler = _make_handler(handler_module)
        _create(handler_module, handler, "main", "beauty", "exr (dwaa 16bit)")
        main_path = handler_module["created"]["Write1"]["file"].value()

        _create(handler_module, handler, "review", "beauty", "mov (h264)")
        review_path = handler_module["created"]["Write1"]["file"].value()

        assert main_path != review_path
        assert main_path.endswith(".exr")
        assert review_path.endswith(".mov")
