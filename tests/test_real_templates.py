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


def test_autopilot_resolves_real_paths(monkeypatch, templates):
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)
    env, hm, holder = fake_nuke.install(monkeypatch)
    holder.app = RealApp(templates)
    plate = env.add("Read", "plate_STRM_E1_0070", 0, 0)
    env.add("Grade", "Grade1", 0, 100, [plate])
    env.root.script_path = script_path(3)

    handler = hm.NukeWriteNodeHandler()
    assert sorted(handler.ensure_auto_write_nodes()) == ["Write_main", "Write_review"]

    files = {
        n.name(): n.children["Write1"]["file"].value()
        for n in env.nodes
        if n.children
    }
    root = "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp"
    assert files["Write_main"] == (
        root + "/renders/main/main/v003/STRM_E1_0070_Comp_main_main_v003.%04d.exr"
    )
    assert files["Write_review"] == (
        root + "/previews/STRM_E1_0070_Comp_review_v003.%04d.mov"
    )

    # version-up follows
    env.root.script_path = script_path(4)
    assert handler.sync_all() == 2
    assert "_v004." in env.nodes[-1].children["Write1"]["file"].value()
