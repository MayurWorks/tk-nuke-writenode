"""
The autopilot against REAL Toolkit templates: tk-core's own Template
objects built from the config repo's templates.yml. Guards the failure the
fake templates cannot: the work template has no {name}, the render
templates require it, and a real apply_fields() raises TankError.

Skipped unless TK_CORE_PYTHON (a tk-core checkout's python/ folder) and
NFA_CFG_CORE (the config repo's core/ folder) exist - by default it looks
for them in /home/claude.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

TK_CORE = os.environ.get("TK_CORE_PYTHON", "/home/claude/tk-core/python")
CFG = os.environ.get("NFA_CFG_CORE", "/home/claude/nfa-shotgun-configuration/core")

pytestmark = pytest.mark.skipif(
    not (os.path.isdir(os.path.join(TK_CORE, "tank")) and os.path.isfile(os.path.join(CFG, "templates.yml"))),
    reason="needs a tk-core checkout and the config repo (see module docstring)",
)

import fake_nuke  # noqa: E402
from fake_nuke import script_path  # noqa: E402


@pytest.fixture(scope="module")
def templates():
    import real_templates

    return real_templates.load_templates()


class RealApp(fake_nuke.FakeApp):
    def __init__(self, templates, settings=None):
        fake_nuke.FakeApp.__init__(self, settings=settings)
        self._templates = templates

    def get_template(self, setting_name):
        # app setting -> template, as the env YAML would map it
        return self._templates[{"template_script_work": "nuke_shot_work"}[setting_name]]

    def get_template_by_name(self, name):
        return self._templates[name]


def test_real_render_templates_need_a_name_the_work_template_lacks(templates):
    fields = templates["nuke_shot_work"].get_fields(script_path(3))
    assert "name" not in fields
    fields.update(SEQ="FORMAT: %d", output="main")
    with pytest.raises(Exception):
        templates["nuke_shot_render"].apply_fields(fields)


def test_on_demand_write_resolves_real_paths(monkeypatch, templates):
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)
    env, hm, holder = fake_nuke.install(monkeypatch)
    holder.app = RealApp(
        templates, settings={"follow_script_version": True, "default_category": "main"}
    )
    plate = env.add("Read", "plate_STRM_E1_0070", 0, 0)
    env.add("Grade", "Grade1", 0, 100, [plate])
    env.root.script_path = script_path(3)

    handler = hm.NukeWriteNodeHandler()
    node = handler.create_writenode_auto()

    root = "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp"
    assert node["file"].value() == (
        root + "/renders/main/main/v003/STRM_E1_0070_Comp_main_main_v003.%04d.exr"
    )

    # the review preset resolves through the movie template
    node["sg_category"].setValue("review")
    handler.knob_changed(node, node["sg_category"])
    assert node["file"].value() == (
        root + "/previews/STRM_E1_0070_comp_OS_v003.mov"
    )

    # version-up follows
    env.root.script_path = script_path(4)
    assert handler.sync_all() == 1
    assert "_v004." in node["file"].value()


def test_shipped_shot_config_drives_the_write_nodes(monkeypatch, templates):
    """Feeds the config repo's own tk-nuke-writenode.yml (shot settings)
    through the handler, so a typo in the YAML shows up here."""
    import yaml

    yml = os.path.join(os.path.dirname(CFG), "env", "includes", "settings", "tk-nuke-writenode.yml")
    if not os.path.isfile(yml):
        pytest.skip("config repo's env/ folder not found")
    shot = yaml.safe_load(open(yml))["settings.tk-nuke-writenode.shot"]

    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)
    env, hm, holder = fake_nuke.install(monkeypatch)
    settings = {k: shot[k] for k in shot if k not in ("location", "template_script_work")}
    holder.app = RealApp(templates, settings=settings)
    plate = env.add("Read", "plate_STRM_E1_0070", 0, 0)
    plate.channel_names = ["rgba.red", "rgba.green", "rgba.blue", "rgba.alpha"]
    env.root.script_path = script_path(1)
    handler = hm.NukeWriteNodeHandler()

    main = handler.create_writenode_auto()
    assert (main["sg_output"].value(), main["sg_category"].value()) == ("main", "main")
    assert main["channels"].value() == "rgba"  # channels: auto, input has alpha
    assert main["compression"].value() == "DWAA"

    pre = handler.create_writenode_auto()
    assert pre["sg_output"].value() == "prerender"
    assert pre["sg_data"].values() == ["exr (dwaa 16bit)", "exr (zip 16bit)", "exr (zip 32bit)"]

    pre["sg_data"].setValue("exr (zip 32bit)")
    handler.knob_changed(pre, pre["sg_data"])
    assert pre["datatype"].value() == "32 bit float"

    main["sg_category"].setValue("review")
    handler.knob_changed(main, main["sg_category"])
    assert main["file_type"].value() == "mov"
    assert main["mov64_codec"].value() == "appr"
