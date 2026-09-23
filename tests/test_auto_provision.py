"""
Integration tests for the write node autopilot: the real handler running
against a fake Nuke DAG (see fake_nuke.py) - what gets created, wired,
named, pointed where, and what is left alone.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import fake_nuke  # noqa: E402
from fake_nuke import FakeApp, script_path  # noqa: E402

PLATE = "plate_STRM_E1_0070"


@pytest.fixture
def world(monkeypatch, tmp_path):
    made_dirs = []
    monkeypatch.setattr(os, "makedirs", lambda path, *a, **k: made_dirs.append(path))
    env, hm, holder = fake_nuke.install(monkeypatch)
    app = FakeApp()
    holder.app = app

    # plate Read -> Grade -> Blur (comp tail), Viewer on the tail
    plate = env.add("Read", PLATE, 0, 0)
    plate.channel_names = ["rgba.red", "rgba.green", "rgba.blue"]
    grade = env.add("Grade", "Grade1", 0, 100, [plate])
    blur = env.add("Blur", "Blur1", 0, 200, [grade])
    blur.channel_names = ["rgba.red", "rgba.green", "rgba.blue", "rgba.alpha"]
    env.add("Viewer", "Viewer1", 0, 300, [blur])
    env.root.script_path = script_path(1)

    handler = hm.NukeWriteNodeHandler()
    return types_ns(env=env, hm=hm, holder=holder, app=app, handler=handler,
                    blur=blur, plate=plate, made_dirs=made_dirs, tmp_path=tmp_path)


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


def write_nodes(env):
    return [n for n in env.nodes if n.knob("isShotGridWriteNode") is not None]


def inner(node):
    return node.children["Write1"]


class TestProvisioning(object):
    def test_creates_main_and_review_wired_to_the_end_of_the_comp(self, world):
        created = world.handler.ensure_auto_write_nodes()
        assert sorted(created) == ["Write_main", "Write_review"]

        main = world.env.nodes and [n for n in world.env.nodes if n.name() == "Write_main"][0]
        review = [n for n in world.env.nodes if n.name() == "Write_review"][0]
        assert main.input(0) is world.blur
        assert review.input(0) is world.blur
        # side by side, below the tail
        assert main.ypos() == review.ypos() == world.blur.ypos() + 90
        assert main.xpos() != review.xpos()
        # nothing selected/connected by accident: only Blur feeds them
        assert [n.name() for n in world.env.nodes if n.input(0) is world.plate] == ["Grade1"]

    def test_paths_versions_and_labels(self, world):
        world.handler.ensure_auto_write_nodes()
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        review = [n for n in world.env.nodes if n.name() == "Write_review"][0]

        assert inner(main)["file"].value() == (
            "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp/renders/"
            "main/main/v001/STRM_E1_0070_Comp_main_main_v001.%04d.exr"
        )
        assert inner(review)["file"].value() == (
            "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp/previews/"
            "STRM_E1_0070_Comp_review_v001.%04d.mov"
        )
        assert inner(main)["file_type"].value() == "exr"
        assert inner(review)["file_type"].value() == "mov"
        assert main["label"].value() == "v001"
        assert main["output"].value() == "main"
        assert main["category"].value() == "main"

    def test_no_render_folders_are_created_just_by_provisioning(self, world):
        world.handler.ensure_auto_write_nodes()
        assert world.made_dirs == []
        # Nuke makes the folder itself at render time
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        assert inner(main)["create_directories"].value() is True

    def test_channels_auto_follows_the_alpha_of_the_input(self, world):
        world.handler.ensure_auto_write_nodes()
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        assert inner(main)["channels"].value() == "rgba"  # Blur1 carries alpha

    def test_channels_auto_without_alpha_is_rgb(self, world):
        world.blur.channel_names = ["rgba.red", "rgba.green", "rgba.blue"]
        world.handler.ensure_auto_write_nodes()
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        assert inner(main)["channels"].value() == "rgb"

    def test_studio_template_anchor_wins(self, world):
        anchor = world.env.add("NoOp", "writeNoOp", 50, 800, [world.blur])
        world.handler.ensure_auto_write_nodes()
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        assert main.input(0) is anchor

    def test_second_run_does_nothing(self, world):
        world.handler.ensure_auto_write_nodes()
        before = len(world.env.nodes)
        assert world.handler.ensure_auto_write_nodes() == []
        assert len(world.env.nodes) == before

    def test_a_deleted_write_node_stays_deleted(self, world):
        world.handler.ensure_auto_write_nodes()
        review = [n for n in world.env.nodes if n.name() == "Write_review"][0]
        world.env.delete(review)
        assert world.handler.ensure_auto_write_nodes() == []
        assert [n.name() for n in write_nodes(world.env)] == ["Write_main"]
        assert world.env.root["sx_writenode_provisioned"].value() == "main,review"

    def test_a_hand_made_category_is_not_duplicated(self, world):
        world.handler.ensure_auto_write_nodes()
        world.handler.ensure_auto_write_nodes()
        world.env.root._knobs.pop("sx_writenode_provisioned")  # e.g. an old script
        assert world.handler.ensure_auto_write_nodes() == []
        assert len(write_nodes(world.env)) == 2

    def test_unsaved_script_is_left_alone(self, world):
        world.env.root.script_path = "Root"
        assert world.handler.ensure_auto_write_nodes() == []

    def test_script_outside_the_work_template_is_left_alone(self, world):
        world.env.root.script_path = "/somewhere/else/test.nk"
        assert world.handler.ensure_auto_write_nodes() == []

    def test_empty_script_waits_for_the_template(self, world):
        for node in list(world.env.nodes):
            world.env.delete(node)
        assert world.handler.ensure_auto_write_nodes() == []

    def test_switched_off_by_default(self, world):
        world.app._settings["auto_provision"] = False
        assert world.handler.ensure_auto_write_nodes() == []

    def test_unconfigured_category_is_skipped_not_fatal(self, world):
        world.app._settings["auto_provision_categories"] = ["main", "nonsense"]
        assert world.handler.ensure_auto_write_nodes() == ["Write_main"]

    def test_before_save_uses_the_save_as_target_not_the_current_path(self, world):
        world.env.root.script_path = "Root"
        world.handler.on_before_save(script_path(7))
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        assert "v007" in inner(main)["file"].value()
        assert main["label"].value() == "v007"

    def test_lone_plate_is_used_when_there_is_no_comp_yet(self, world, monkeypatch):
        for node in [n for n in world.env.nodes if n is not world.plate]:
            world.env.delete(node)
        world.handler.ensure_auto_write_nodes()
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        assert main.input(0) is world.plate


class TestSync(object):
    def test_version_up_repoints_every_write_node(self, world):
        world.handler.ensure_auto_write_nodes()
        world.env.root.script_path = script_path(2)
        assert world.handler.sync_all() == 2
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        review = [n for n in world.env.nodes if n.name() == "Write_review"][0]
        assert "/v002/" in inner(main)["file"].value()
        assert "_v002." in inner(review)["file"].value()
        assert main["label"].value() == "v002"

    def test_sync_is_a_no_op_when_nothing_changed(self, world):
        world.handler.ensure_auto_write_nodes()
        world.handler.sync_all()
        counts = {
            (n.name(), k): knob.sets
            for n in world.env.nodes
            if n.children
            for k, knob in inner(n)._knobs.items()
        }
        world.handler.sync_all()
        after = {
            (n.name(), k): knob.sets
            for n in world.env.nodes
            if n.children
            for k, knob in inner(n)._knobs.items()
        }
        assert counts == after

    def test_sync_never_creates_folders(self, world):
        world.handler.ensure_auto_write_nodes()
        world.env.root.script_path = script_path(3)
        world.handler.sync_all()
        assert world.made_dirs == []

    def test_sync_leaves_artist_tuned_knobs_alone(self, world):
        world.handler.ensure_auto_write_nodes()
        main = [n for n in world.env.nodes if n.name() == "Write_main"][0]
        inner(main)["compression"].setValue("Zip (16 scanlines)")
        world.env.root.script_path = script_path(2)
        world.handler.sync_all()
        assert inner(main)["compression"].value() == "Zip (16 scanlines)"

    def test_movie_fps_follows_the_script_fps(self, world):
        world.handler.ensure_auto_write_nodes()
        review = [n for n in world.env.nodes if n.name() == "Write_review"][0]
        assert inner(review)["mov64_fps"].value() == 24.0  # not the YAML's 24
        world.env.root["fps"].setValue(25.0)
        world.handler.sync_all()
        assert inner(review)["mov64_fps"].value() == 25.0

    def test_unsaved_script_syncs_nothing(self, world):
        world.handler.ensure_auto_write_nodes()
        world.env.root.script_path = "Root"
        assert world.handler.sync_all() == 0

    def test_only_shotgrid_write_nodes_are_touched(self, world):
        plain = world.env.add("Group", "SomeOtherGroup", 300, 0)
        world.handler.ensure_auto_write_nodes()
        world.handler.sync_all()
        assert plain._knobs == {}


class TestScheduling(object):
    def test_callbacks_registered_and_removed(self, world):
        world.handler.add_callbacks()
        kinds = sorted(set(k for k, _, _ in world.env.callbacks))
        assert kinds == ["KnobChanged", "OnCreate", "OnScriptLoad", "OnScriptSave"]
        assert all(nc == "Root" for _, _, nc in world.env.callbacks)
        world.handler.remove_callbacks()
        assert world.env.callbacks == []

    def test_triggers_defer_instead_of_running_inline(self, world):
        world.handler._on_script_load()
        assert write_nodes(world.env) == []  # nothing yet: deferred
        assert len(world.env.deferred) == 1
        call, args, kwargs = world.env.deferred[0]
        call(*args, **kwargs)
        assert len(write_nodes(world.env)) == 2

    def test_headless_sessions_never_edit_the_script(self, world, monkeypatch):
        monkeypatch.setattr(sys.modules["nuke"], "GUI", False)
        world.handler._on_script_load()
        world.handler._on_root_create()
        assert world.env.deferred == []

    def test_only_a_rename_of_the_script_triggers_a_sync(self, world):
        world.env.this_knob = fake_nuke.Knob("first_frame")
        world.handler._on_root_knob_changed()
        assert world.env.deferred == []
        world.env.this_knob = fake_nuke.Knob("name")
        world.handler._on_root_knob_changed()
        assert len(world.env.deferred) == 1

    def test_autopilot_failure_never_propagates(self, world, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(world.handler, "ensure_auto_write_nodes", boom)
        world.handler.run_autopilot("test")  # must not raise


class TestQuickCreate(object):
    def test_w_creates_the_default_category_without_a_dialog(self, world):
        node = world.handler.create_writenode_auto()
        assert node.name() == "Write_prerender"
        assert node.input(0) is world.blur  # nothing selected -> end of comp
        assert node["category"].value() == "prerender"

    def test_w_wires_to_the_selected_node_and_counts_up(self, world):
        grade = [n for n in world.env.nodes if n.name() == "Grade1"][0]
        grade.selected = True
        first = world.handler.create_writenode_auto()
        assert first.input(0) is grade
        assert first.ypos() == grade.ypos() + 100

        second = world.handler.create_writenode_auto()
        assert second.name() == "Write_prerender2"

    def test_w_after_autopilot_still_hangs_off_the_comp_tail(self, world):
        world.handler.ensure_auto_write_nodes()
        node = world.handler.create_writenode_auto()
        assert node.input(0) is world.blur

    def test_w_falls_back_to_any_free_category(self, world):
        world.app._settings["default_category"] = "does-not-exist"
        node = world.handler.create_writenode_auto()
        assert node["category"].value() == "main"
        # main is now taken, so the next one moves on
        assert world.handler.create_writenode_auto()["category"].value() == "prerender"

    def test_unsaved_script_still_creates_the_node_quietly(self, world):
        world.env.root.script_path = "Root"
        node = world.handler.create_writenode_auto()
        assert node is not None
        assert world.env.messages == []
        assert inner(node)["file"].value() is None  # filled in once saved


class TestPerProjectCategories(object):
    def test_custom_pipeline_keeps_the_review_category(self, world):
        world.app.shotgun.pipeline = "rec709_sdr"
        options = world.handler._NukeWriteNodeHandler__get_write_node_options()
        assert set(options) == {"main", "prerender", "review"}

    def test_review_colorspace_matches_the_ocio_config(self, world):
        world.app.shotgun.pipeline = "rec709_sdr"
        world.handler.ensure_auto_write_nodes()
        review = [n for n in world.env.nodes if n.name() == "Write_review"][0]
        assert inner(review)["colorspace"].value() == "rec709"
        # and the static setting was not mutated
        review_cfg = [c for c in fake_nuke.CATEGORIES if c["category_name"] == "review"][0]
        assert review_cfg["write_nodes"][0]["settings"]["colorspace"] == "Output - Rec.709"

    def test_pipeline_lookup_is_cached(self, world):
        world.app.shotgun.pipeline = "aces_acescg"
        world.handler.ensure_auto_write_nodes()
        world.handler.sync_all()
        world.handler.sync_all()
        assert world.app.shotgun.calls == 1


class TestAutoRead(object):
    def _render_ready(self, monkeypatch, tmp_path):
        env, hm, holder = fake_nuke.install(monkeypatch)
        app = FakeApp(settings={"auto_read_after_render": True}, root=str(tmp_path))
        holder.app = app
        plate = env.add("Read", PLATE, 0, 0)
        env.root.script_path = script_path(1, root=str(tmp_path))
        handler = hm.NukeWriteNodeHandler()
        handler.ensure_auto_write_nodes()
        return env, handler, app

    def _fake_frames(self, env, names=("main",)):
        node = [n for n in env.nodes if n.name() == "Write_main"][0]
        path = inner(node)["file"].value()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for frame in (1001, 1002, 1003):
            open(path % frame, "w").close()
        return node

    def test_local_render_leaves_a_read_node_and_reuses_it(self, monkeypatch, tmp_path):
        env, handler, app = self._render_ready(monkeypatch, tmp_path)
        node = self._fake_frames(env)

        handler.render_local(node)
        reads = [n for n in env.nodes if n.name() == "render_main"]
        assert len(reads) == 1
        read = reads[0]
        assert read["file"].value() == inner(node)["file"].value()
        assert (read["first"].value(), read["last"].value()) == (1001, 1003)
        assert read["colorspace"].value() == "ACES - ACEScg"

        handler.render_local(node)  # again: refreshed, not duplicated
        assert len([n for n in env.nodes if n.name().startswith("render_main")]) == 1

    def test_switched_off_creates_nothing(self, monkeypatch, tmp_path):
        env, handler, app = self._render_ready(monkeypatch, tmp_path)
        app._settings["auto_read_after_render"] = False
        node = self._fake_frames(env)
        handler.render_local(node)
        assert [n for n in env.nodes if n.name() == "render_main"] == []

    def test_movies_are_not_read_back(self, monkeypatch, tmp_path):
        env, handler, app = self._render_ready(monkeypatch, tmp_path)
        review = [n for n in env.nodes if n.name() == "Write_review"][0]
        handler.render_local(review)
        assert [n for n in env.nodes if n.name().startswith("render_")] == []
