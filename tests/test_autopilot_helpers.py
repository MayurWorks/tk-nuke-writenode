"""Unit tests for the pure decision helpers in autopilot.py (no Nuke)."""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "autopilot",
    os.path.join(
        os.path.dirname(__file__), "..", "python", "tk_nuke_writenode", "autopilot.py"
    ),
)
autopilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autopilot)


class N(object):
    """Bare-bones node for pick_tail()."""

    def __init__(self, name, cls="Grade", y=0, inputs=()):
        self._name, self._cls, self._y, self.inputs = name, cls, y, list(inputs)

    def name(self):
        return self._name

    def Class(self):
        return self._cls

    def ypos(self):
        return self._y


def _graph(nodes):
    def dependencies(node):
        return node.inputs

    def dependents(node):
        return [n for n in nodes if node in n.inputs]

    return dict(dependents=dependents, dependencies=dependencies)


class TestNames(object):
    def test_main_only_once(self):
        assert autopilot.next_output_name("main", []) == "main"
        assert autopilot.next_output_name("main", ["main"]) is None

    def test_other_categories_count_up(self):
        assert autopilot.next_output_name("review", []) == "review"
        assert autopilot.next_output_name("review", ["review"]) == "review2"
        assert autopilot.next_output_name("review", ["review", "review2"]) == "review3"

    def test_names_are_alphanumeric_for_the_template_key(self):
        assert autopilot.next_output_name("pre_render", []) == "prerender"
        assert autopilot.next_output_name("___", []) is None

    def test_custom_main_names(self):
        assert autopilot.next_output_name("beauty", [], "beauty", "hero") == "hero"


class TestProvisionedMarker(object):
    def test_round_trip(self):
        assert autopilot.parse_provisioned("") == set()
        assert autopilot.parse_provisioned(None) == set()
        value = autopilot.format_provisioned({"review", "main"})
        assert value == "main,review"
        assert autopilot.parse_provisioned(value) == {"main", "review"}


class TestSmallHelpers(object):
    def test_channels(self):
        assert autopilot.channels_for(["rgba.red", "rgba.alpha"]) == "rgba"
        assert autopilot.channels_for(["rgba.red"]) == "rgb"
        assert autopilot.channels_for(None) == "rgb"

    def test_fps(self):
        assert autopilot.valid_fps(25) == 25.0
        assert autopilot.valid_fps("23.976") == 23.976
        assert autopilot.valid_fps(0) is None
        assert autopilot.valid_fps(None) is None
        assert autopilot.valid_fps("abc") is None


class TestPickTail(object):
    def test_prefers_the_comp_downstream_of_the_plate(self):
        plate = N("plate_S", "Read", 0)
        grade = N("Grade1", "Grade", 100, [plate])
        blur = N("Blur1", "Blur", 200, [grade])
        stray = N("Stray", "Grade", 900)  # lower in the DAG, not on the plate
        nodes = [plate, grade, blur, stray]
        tail = autopilot.pick_tail(nodes, "plate_S", **_graph(nodes))
        assert tail is blur

    def test_a_lone_plate_is_the_last_resort(self):
        plate = N("plate_S", "Read", 0)
        assert autopilot.pick_tail([plate], "plate_S", **_graph([plate])) is plate

    def test_comp_skeleton_beats_bare_plate(self):
        plate = N("plate_S", "Read", 0)
        dot = N("Dot4", "Dot", 500)
        nodes = [plate, dot]
        assert autopilot.pick_tail(nodes, "plate_S", **_graph(nodes)) is dot

    def test_viewer_does_not_make_a_node_non_terminal(self):
        plate = N("plate_S", "Read", 0)
        grade = N("Grade1", "Grade", 100, [plate])
        viewer = N("Viewer1", "Viewer", 200, [grade])
        nodes = [plate, grade, viewer]
        assert autopilot.pick_tail(nodes, "plate_S", **_graph(nodes)) is grade

    def test_existing_write_nodes_are_skipped(self):
        plate = N("plate_S", "Read", 0)
        write = N("Write_main", "Group", 300, [plate])
        nodes = [plate, write]
        tail = autopilot.pick_tail(
            nodes,
            "plate_S",
            is_write=lambda n: n is write,
            **_graph(nodes)
        )
        # the write hanging off the plate does not make it "not the end"
        assert tail is plate

    def test_tail_is_stable_after_write_nodes_were_attached_to_it(self):
        plate = N("plate_S", "Read", 0)
        blur = N("Blur1", "Blur", 100, [plate])
        w1 = N("Write_main", "Group", 190, [blur])
        w2 = N("Write_review", "Group", 190, [blur])
        nodes = [plate, blur, w1, w2]
        tail = autopilot.pick_tail(
            nodes, "plate_S", is_write=lambda n: n in (w1, w2), **_graph(nodes)
        )
        assert tail is blur

    def test_nothing_to_attach_to(self):
        viewer = N("Viewer1", "Viewer")
        backdrop = N("Backdrop1", "BackdropNode")
        assert autopilot.pick_tail([viewer, backdrop]) is None
        assert autopilot.pick_tail([]) is None
