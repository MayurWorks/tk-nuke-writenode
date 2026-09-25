"""
On-demand write nodes: the real handler running against a fake Nuke DAG
(see fake_nuke.py). A write node is a plain Write with an "NFA ShotGrid"
tab; it appears only when asked for ("w") and afterwards belongs to the
artist.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import fake_nuke  # noqa: E402
from fake_nuke import FakeApp, script_path  # noqa: E402

PLATE = "plate_STRM_E1_0070"


@pytest.fixture
def world(monkeypatch):
    made_dirs = []
    monkeypatch.setattr(os, "makedirs", lambda path, *a, **k: made_dirs.append(path))
    env, hm, holder = fake_nuke.install(monkeypatch)
    app = FakeApp(settings={"follow_script_version": True, "default_category": "main"})
    holder.app = app

    # plate Read -> Grade -> Blur (comp tail), Viewer on the tail
    plate = env.add("Read", PLATE, 0, 0)
    plate.channel_names = ["rgba.red", "rgba.green", "rgba.blue"]
    grade = env.add("Grade", "Grade1", 0, 100, [plate])
    blur = env.add("Blur", "Blur1", 0, 200, [grade])
    blur.channel_names = ["rgba.red", "rgba.green", "rgba.blue", "rgba.alpha"]
    env.add("Viewer", "Viewer1", 0, 300, [blur])
    env.root.script_path = script_path(1)

    return types.SimpleNamespace(
        env=env,
        hm=hm,
        holder=holder,
        app=app,
        handler=hm.NukeWriteNodeHandler(),
        blur=blur,
        grade=grade,
        plate=plate,
        made_dirs=made_dirs,
    )


def sg_writes(env):
    return [n for n in env.nodes if n.knob("isShotGridWriteNode") is not None]


def switch(world, node, knob, value):
    """The artist changes a knob and Nuke fires knobChanged."""
    node[knob].setValue(value)
    world.handler.knob_changed(node, node[knob])


class TestPressW(object):
    def test_creates_a_plain_write_wired_to_the_end_of_the_comp(self, world):
        node = world.handler.create_writenode_auto()
        assert node.Class() == "Write"
        assert node.children == {}  # no group, no gizmo
        assert node.input(0) is world.blur
        assert node.ypos() == world.blur.ypos() + 100
        assert sg_writes(world.env) == [node]

    def test_paths_settings_and_label_for_this_shot(self, world):
        node = world.handler.create_writenode_auto()
        assert node["file"].value() == (
            "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp/renders/"
            "main/main/v001/STRM_E1_0070_Comp_main_main_v001.%04d.exr"
        )
        assert node["file_type"].value() == "exr"
        assert node["colorspace"].value() == "ACES - ACEScg"
        assert node["compression"].value() == "DWAA"
        assert node["create_directories"].value() is True
        assert node["tile_color"].value() == 4292673791
        assert node["label"].value() == "main v001"
        assert node["sg_output"].value() == "main"
        assert node["sg_category"].value() == "main"
        assert node["sg_data"].value() == "exr (dwaa 16bit)"
        assert node["sg_path"].value() == node["file"].value()

    def test_channels_auto_follows_alpha_of_the_input(self, world):
        assert world.handler.create_writenode_auto()["channels"].value() == "rgba"
        world.blur.channel_names = ["rgba.red", "rgba.green", "rgba.blue"]
        world.env.delete(sg_writes(world.env)[0])
        assert world.handler.create_writenode_auto()["channels"].value() == "rgb"

    def test_the_node_carries_its_studio_tab(self, world):
        node = world.handler.create_writenode_auto()
        for button, method in (
            ("sg_render_local", "render_local"),
            ("sg_render_farm", "render_farm"),
            ("sg_read", "read_from_write"),
            ("sg_apply", "apply_write_settings"),
        ):
            assert method in node[button].command
            assert "tk-nuke-writenode" in node[button].command
        assert "knob_changed" in node["knobChanged"].value()

    def test_repeated_presses_give_main_then_prerenders(self, world):
        outputs = [world.handler.create_writenode_auto()["sg_output"].value() for _ in range(4)]
        assert outputs == ["main", "prerender", "prerender2", "prerender3"]

    def test_wires_to_the_selected_node(self, world):
        world.grade.selected = True
        node = world.handler.create_writenode_auto()
        assert node.input(0) is world.grade
        assert node.ypos() == world.grade.ypos() + 100

    def test_w_after_other_writes_still_hangs_off_the_comp_tail(self, world):
        world.handler.create_writenode_auto()
        assert world.handler.create_writenode_auto().input(0) is world.blur

    def test_lone_plate_when_there_is_no_comp_yet(self, world):
        for node in [n for n in world.env.nodes if n is not world.plate]:
            world.env.delete(node)
        assert world.handler.create_writenode_auto().input(0) is world.plate

    def test_studio_template_anchor_wins(self, world):
        anchor = world.env.add("NoOp", "writeNoOp", 50, 800, [world.blur])
        assert world.handler.create_writenode_auto().input(0) is anchor

    def test_unsaved_script_still_creates_the_node_quietly(self, world):
        world.env.root.script_path = "Root"
        node = world.handler.create_writenode_auto()
        assert node is not None
        assert world.env.messages == []
        assert not node["file"].value()

    def test_no_render_folder_until_something_renders(self, world):
        world.handler.create_writenode_auto()
        assert world.made_dirs == []

    def test_unconfigured_default_falls_back_to_a_free_category(self, world):
        world.app._settings["default_category"] = "nope"
        assert world.handler.create_writenode_auto()["sg_category"].value() == "main"


class TestNothingIsCreatedBehindTheArtistsBack(object):
    def test_opening_saving_and_renaming_never_create_a_node(self, world):
        world.handler.add_callbacks()
        before = list(world.env.nodes)
        world.handler._on_script_save()
        world.handler.on_before_save(script_path(2))
        world.env.this_knob = fake_nuke.Knob("name")
        world.handler._on_root_knob_changed()
        for call, args, kwargs in world.env.deferred:
            call(*args, **kwargs)
        assert world.env.nodes == before

    def test_only_save_and_rename_callbacks_are_registered(self, world):
        world.handler.add_callbacks()
        kinds = sorted(k for k, _, _ in world.env.callbacks)
        assert kinds == ["KnobChanged", "OnScriptLoad", "OnScriptSave"]
        world.handler.remove_callbacks()
        assert world.env.callbacks == []

    def test_follow_script_version_can_be_switched_off(self, world):
        world.app._settings["follow_script_version"] = False
        node = world.handler.create_writenode_auto()
        world.handler.add_callbacks()
        assert [k for k, _, _ in world.env.callbacks] == ["OnScriptLoad"]
        world.env.root.script_path = script_path(2)
        assert world.handler.sync_all() == 0
        assert "v001" in node["file"].value()


class TestPathFollowsTheScript(object):
    def test_version_up_repoints_the_path_and_label(self, world):
        node = world.handler.create_writenode_auto()
        world.env.root.script_path = script_path(2)
        assert world.handler.sync_all() == 1
        assert "/v002/" in node["file"].value()
        assert node["label"].value() == "main v002"
        assert node["sg_path"].value() == node["file"].value()

    def test_before_save_uses_the_save_as_target(self, world):
        node = world.handler.create_writenode_auto()
        world.handler.on_before_save(script_path(7))
        assert "/v007/" in node["file"].value()

    def test_root_rename_syncs_in_the_background(self, world):
        node = world.handler.create_writenode_auto()
        world.env.root.script_path = script_path(3)
        world.env.this_knob = fake_nuke.Knob("first_frame")
        world.handler._on_root_knob_changed()
        assert world.env.deferred == []
        world.env.this_knob = fake_nuke.Knob("name")
        world.handler._on_root_knob_changed()
        call, args, kwargs = world.env.deferred[0]
        call(*args, **kwargs)
        assert "/v003/" in node["file"].value()

    def test_a_hand_edited_path_is_left_alone(self, world):
        node = world.handler.create_writenode_auto()
        node["file"].setValue("/my/own/place/shot.%04d.exr")
        world.env.root.script_path = script_path(2)
        world.handler.sync_all()
        assert node["file"].value() == "/my/own/place/shot.%04d.exr"

    def test_only_the_path_moves_artist_settings_stay(self, world):
        node = world.handler.create_writenode_auto()
        node["compression"].setValue("Zip (16 scanlines)")
        node["colorspace"].setValue("ACES - ACES2065-1")
        node["channels"].setValue("all")
        world.env.root.script_path = script_path(2)
        world.handler.sync_all()
        assert node["compression"].value() == "Zip (16 scanlines)"
        assert node["colorspace"].value() == "ACES - ACES2065-1"
        assert node["channels"].value() == "all"

    def test_an_artists_own_label_is_kept(self, world):
        node = world.handler.create_writenode_auto()
        node["label"].setValue("HERO PASS")
        world.env.root.script_path = script_path(2)
        world.handler.sync_all()
        assert node["label"].value() == "HERO PASS"

    def test_sync_is_a_no_op_when_nothing_changed(self, world):
        node = world.handler.create_writenode_auto()
        world.handler.sync_all()
        sets = {k: v.sets for k, v in node._knobs.items()}
        world.handler.sync_all()
        assert sets == {k: v.sets for k, v in node._knobs.items()}

    def test_render_refreshes_the_path_and_makes_the_folder(self, world):
        node = world.handler.create_writenode_auto()
        world.env.root.script_path = script_path(2)
        world.handler.render_local(node)
        assert "/v002/" in node["file"].value()
        assert world.made_dirs == [os.path.dirname(node["file"].value())]
        assert node["Render"].sets == 1

    def test_render_does_not_touch_settings_the_artist_changed(self, world):
        node = world.handler.create_writenode_auto()
        node["compression"].setValue("PIZ")
        world.handler.render_local(node)
        assert node["compression"].value() == "PIZ"

    def test_unsaved_script_and_foreign_scripts_are_skipped(self, world):
        world.handler.create_writenode_auto()
        world.env.root.script_path = "Root"
        assert world.handler.sync_all() == 0
        world.env.root.script_path = "/somewhere/else.nk"
        assert world.handler.sync_all() == 0

    def test_only_shotgrid_write_nodes_are_touched(self, world):
        plain = world.env.add("Write", "Write9", 300, 0)
        plain["file"].setValue("/x/y.%04d.exr")
        world.handler.create_writenode_auto()
        world.handler.sync_all()
        assert plain["file"].value() == "/x/y.%04d.exr"
        assert plain.knob("isShotGridWriteNode") is None

    def test_sync_failure_never_propagates(self, world, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(world.handler, "sync_all", boom)
        world.handler.run_sync("test")  # must not raise

    def test_headless_sessions_never_schedule_anything(self, world, monkeypatch):
        monkeypatch.setattr(sys.modules["nuke"], "GUI", False)
        world.handler.schedule_sync("x")
        world.handler._on_script_save()
        assert world.env.deferred == []


class TestPickingAnotherPreset(object):
    def test_switching_to_review_swaps_presets_type_colorspace_and_path(self, world):
        node = world.handler.create_writenode_auto()
        switch(world, node, "sg_category", "review")
        assert node["sg_data"].values() == ["mov (review)"]
        assert node["sg_data"].value() == "mov (review)"
        assert node["file_type"].value() == "mov"
        assert node["colorspace"].value() == "Output - Rec.709"
        assert node["file"].value() == (
            "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp/previews/"
            "STRM_E1_0070_comp_OS_v001.mov"
        )

    def test_review_mov_runs_at_the_script_fps(self, world):
        world.env.root["fps"].setValue(25.0)
        node = world.handler.create_writenode_auto()
        switch(world, node, "sg_category", "review")
        assert node["mov64_fps"].value() == 25.0

    def test_review_mov_gets_the_studio_codec_not_the_presets(self, world):
        """CATEGORIES' "review" preset still says mov64_codec: H.264 and
        mov64_quality_max: 3 (a not-yet-updated preset) - the app's
        movie_codec setting (Apple ProRes 422 HQ in this fixture) must win,
        and the H.264-only quality knob must not end up on the node."""
        node = world.handler.create_writenode_auto()
        switch(world, node, "sg_category", "review")
        assert node["mov64_codec"].value() == "Apple ProRes 422 HQ"
        assert "mov64_quality_max" not in node.knobs()

    def test_review_mov_codec_falls_back_when_app_setting_is_absent(self, world):
        """An app deployed before movie_codec existed (get_setting raises for
        an unknown setting name) still gets ProRes, not whatever a stale
        preset says - DEFAULT_MOVIE_CODEC, not silence or a KeyError."""
        del world.app._settings["movie_codec"]
        node = world.handler.create_writenode_auto()
        switch(world, node, "sg_category", "review")
        assert node["mov64_codec"].value() == world.hm.DEFAULT_MOVIE_CODEC

    def test_output_name_is_sanitised_and_moves_the_path(self, world):
        node = world.handler.create_writenode_auto()
        switch(world, node, "sg_output", "hero_v2!")
        assert node["sg_output"].value() == "herov2"
        assert "/herov2/" in node["file"].value()

    def test_apply_studio_settings_resets_what_the_artist_changed(self, world):
        node = world.handler.create_writenode_auto()
        node["compression"].setValue("PIZ")
        node["file"].setValue("/my/own/place.%04d.exr")
        world.handler.apply_write_settings(node)
        assert node["compression"].value() == "DWAA"
        assert node["file"].value().startswith("/jobs/SlateX/Artists/")

    def test_unrelated_knob_changes_do_nothing(self, world):
        node = world.handler.create_writenode_auto()
        node["compression"].setValue("PIZ")
        world.handler.knob_changed(node, node["compression"])
        assert node["compression"].value() == "PIZ"


class TestPerProjectPipeline(object):
    def test_custom_pipeline_keeps_the_review_category(self, world):
        world.app.shotgun.pipeline = "rec709_sdr"
        options = world.handler._NukeWriteNodeHandler__get_write_node_options()
        assert set(options) == {"main", "prerender", "review"}

    def test_review_colorspace_matches_the_ocio_config(self, world):
        world.app.shotgun.pipeline = "rec709_sdr"
        node = world.handler.create_writenode_auto()
        switch(world, node, "sg_category", "review")
        assert node["colorspace"].value() == "rec709"
        review_cfg = [c for c in fake_nuke.CATEGORIES if c["category_name"] == "review"][0]
        assert review_cfg["write_nodes"][0]["settings"]["colorspace"] == "Output - Rec.709"

    def test_pipeline_lookup_is_cached(self, world):
        world.app.shotgun.pipeline = "aces_acescg"
        world.handler.create_writenode_auto()
        world.handler.sync_all()
        world.handler.sync_all()
        assert world.app.shotgun.calls == 1


class TestLegacyGroups(object):
    """Scripts saved with the old sgWrite gizmo keep working."""

    def _legacy(self, world):
        group = world.env.add("Group", "Write_main", 0, 400, [world.blur])
        group._knobs["isShotGridWriteNode"] = fake_nuke.Knob("isShotGridWriteNode")
        group._knobs["output"] = fake_nuke.Knob("output", "main")
        group._knobs["category"] = fake_nuke.Knob("category", "main")
        group._knobs["dataType"] = fake_nuke.Knob("dataType", "exr (dwaa 16bit)")
        group.children["Write1"] = fake_nuke.Node(world.env, "Write", "Write1")
        return group

    def test_listed_and_synced(self, world):
        group = self._legacy(world)
        assert world.handler.get_all_write_nodes() == ["Write_main"]
        assert world.handler.sync_all() == 1
        assert "/v001/" in group.children["Write1"]["file"].value()
        world.env.root.script_path = script_path(2)
        world.handler.sync_all()
        assert "/v002/" in group.children["Write1"]["file"].value()

    def test_colorspace_read_from_the_inner_write(self, world):
        group = self._legacy(world)
        group.children["Write1"]["colorspace"].setValue("ACES - ACEScg")
        assert world.handler.get_colorspace(group) == "ACES - ACEScg"


class TestAutoRead(object):
    def _render_ready(self, monkeypatch, tmp_path):
        env, hm, holder = fake_nuke.install(monkeypatch)
        app = FakeApp(
            settings={
                "auto_read_after_render": True,
                "follow_script_version": True,
                "default_category": "main",
            },
            root=str(tmp_path),
        )
        holder.app = app
        env.add("Read", PLATE, 0, 0)
        env.root.script_path = script_path(1, root=str(tmp_path))
        handler = hm.NukeWriteNodeHandler()
        return env, handler, app

    def _fake_frames(self, node):
        path = node["file"].value()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for frame in (1001, 1002, 1003):
            open(path % frame, "w").close()

    def test_local_render_leaves_a_read_node_and_reuses_it(self, monkeypatch, tmp_path):
        env, handler, app = self._render_ready(monkeypatch, tmp_path)
        node = handler.create_writenode_auto()
        self._fake_frames(node)

        handler.render_local(node)
        reads = [n for n in env.nodes if n.name() == "render_main"]
        assert len(reads) == 1
        read = reads[0]
        assert read["file"].value() == node["file"].value()
        assert (read["first"].value(), read["last"].value()) == (1001, 1003)
        assert read["colorspace"].value() == "ACES - ACEScg"

        handler.render_local(node)  # again: refreshed, not duplicated
        assert len([n for n in env.nodes if n.name().startswith("render_main")]) == 1

    def test_switched_off_creates_nothing(self, monkeypatch, tmp_path):
        env, handler, app = self._render_ready(monkeypatch, tmp_path)
        app._settings["auto_read_after_render"] = False
        node = handler.create_writenode_auto()
        self._fake_frames(node)
        handler.render_local(node)
        assert [n for n in env.nodes if n.name() == "render_main"] == []

    def test_movies_are_not_read_back(self, monkeypatch, tmp_path):
        env, handler, app = self._render_ready(monkeypatch, tmp_path)
        node = handler.create_writenode_auto()
        node["sg_category"].setValue("review")
        handler.knob_changed(node, node["sg_category"])
        handler.render_local(node)
        assert [n for n in env.nodes if n.name().startswith("render_")] == []


class TestWhichTypeOfWrite(object):
    """w asks first: EXR or MOV."""

    def test_w_asks_exr_or_mov_and_exr_is_the_first_choice(self, world):
        node = world.handler.create_writenode_auto()
        assert world.env.choices == [["EXR  (image sequence)", "MOV  (review movie)"]]
        assert node["sg_category"].value() == "main"
        assert node["file_type"].value() == "exr"

    def test_mov_makes_the_review_write_named_the_openslate_way(self, world):
        world.env.choice_answer = 1
        node = world.handler.create_writenode_auto()
        assert node["sg_category"].value() == "review"
        assert node["sg_output"].value() == "review"
        assert node["file_type"].value() == "mov"
        assert node["colorspace"].value() == "Output - Rec.709"
        assert node["file"].value() == (
            "/jobs/SlateX/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp/previews/"
            "STRM_E1_0070_comp_OS_v001.mov"
        )
        assert node["label"].value() == "review v001"
        assert node.input(0) is world.blur
        # the dropdown still offers every category
        assert node["sg_category"].values() == ["main", "prerender", "review"]

    def test_mov_follows_the_script_version_and_fps(self, world):
        world.env.root["fps"].setValue(25.0)
        world.env.choice_answer = 1
        node = world.handler.create_writenode_auto()
        assert node["mov64_fps"].value() == 25.0
        world.env.root.script_path = script_path(3)
        world.handler.sync_all()
        assert node["file"].value().endswith("STRM_E1_0070_comp_OS_v003.mov")

    def test_exr_after_a_mov_still_gets_main_then_prerender(self, world):
        world.env.choice_answer = 1
        world.handler.create_writenode_auto()
        world.env.choice_answer = 0
        outputs = [world.handler.create_writenode_auto()["sg_output"].value() for _ in range(3)]
        assert outputs == ["main", "prerender", "prerender2"]

    def test_cancelling_the_prompt_creates_nothing(self, world):
        before = list(world.env.nodes)
        world.env.choice_answer = -1
        assert world.handler.create_writenode_auto() is None
        assert world.env.nodes == before
        assert world.env.messages == []

    def test_a_second_mov_selects_the_existing_one_instead(self, world):
        world.env.choice_answer = 1
        first = world.handler.create_writenode_auto()
        count = len(world.env.nodes)
        again = world.handler.create_writenode_auto()
        assert again is first
        assert len(world.env.nodes) == count
        assert first.selected
        assert "already has a MOV write node" in world.env.messages[-1]

    def test_kind_argument_skips_the_prompt(self, world):
        node = world.handler.create_writenode_auto(kind="movie")
        assert node["file_type"].value() == "mov"
        assert world.env.choices == []

    def test_no_prompt_when_only_one_kind_is_configured(self, world):
        world.app._settings["categories"] = [
            c for c in fake_nuke.CATEGORIES if c["category_name"] != "review"
        ]
        node = world.handler.create_writenode_auto()
        assert world.env.choices == []
        assert node["file_type"].value() == "exr"


class PickyMenu(object):
    """An Enumeration knob like Nuke's mov64_codec: setValue() with a
    string that is not exactly a menu entry silently does nothing."""

    def __init__(self, value, items):
        self._value, self._items = value, items

    def value(self):
        return self._value

    def values(self):
        return list(self._items)

    def setValue(self, value):
        if value in self._items:
            self._value = value


class TestMenuNamesThatDontMatchExactly(object):
    def _set(self, world, knob, value):
        world.handler._NukeWriteNodeHandler__set_knob({"k": knob}, "k", value)

    def test_codec_label_is_matched_to_the_menu_entry(self, world):
        codec = PickyMenu("Apple ProRes  appr", ["Apple ProRes  appr", "H.264  avc1"])
        self._set(world, codec, "H.264")
        assert codec.value() == "H.264  avc1"

    def test_exact_entries_and_case_differences(self, world):
        menu = PickyMenu("a", ["a", "ACES - ACEScg"])
        self._set(world, menu, "aces - acescg")
        assert menu.value() == "ACES - ACEScg"

    def test_an_unknown_entry_is_left_alone_and_does_not_raise(self, world):
        menu = PickyMenu("a", ["a", "b"])
        self._set(world, menu, "zzz")
        assert menu.value() == "a"

    def test_prores_hq_matches_its_real_menu_entry_not_plain_or_4444(self, world):
        menu = PickyMenu(
            "H.264  avc1",
            [
                "Apple ProRes 4444  ap4h",
                "Apple ProRes 422 HQ  apch",
                "Apple ProRes 422  apcn",
                "Apple ProRes 422 LT  apcs",
                "H.264  avc1",
            ],
        )
        self._set(world, menu, "Apple ProRes 422 HQ")
        assert menu.value() == "Apple ProRes 422 HQ  apch"

    def test_shortest_entry_wins_so_plain_422_does_not_become_422_hq(self, world):
        """Requesting the plain "422" preset must not silently pick 422 HQ
        (or LT) just because both start with "Apple ProRes 422"."""
        menu = PickyMenu(
            "x",
            [
                "Apple ProRes 422 HQ  apch",
                "Apple ProRes 422  apcn",
                "Apple ProRes 422 LT  apcs",
            ],
        )
        self._set(world, menu, "Apple ProRes 422")
        assert menu.value() == "Apple ProRes 422  apcn"

    def test_four_character_codec_code_is_matched_too(self, world):
        menu = PickyMenu(
            "H.264  avc1",
            ["Apple ProRes 422 HQ  apch", "Apple ProRes 4444  ap4h", "H.264  avc1"],
        )
        self._set(world, menu, "apch")
        assert menu.value() == "Apple ProRes 422 HQ  apch"
