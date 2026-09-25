"""
A small fake of just enough of Nuke's Python API to run the write node
autopilot against a real graph of nodes: creating/naming/connecting nodes,
group contexts ("with node:" -> nuke.toNode("Write1") is the group's inner
Write), dependents/dependencies, selection, Root knobs and callbacks.

It is deliberately dumb - it exists so tests exercise the handler's
actual logic (what gets created, wired, named, set) without Nuke.
"""
import os
import re
import sys
import types


class Knob(object):
    def __init__(self, name="", value=None):
        self._name = name
        self._value = value
        self.sets = 0
        self.flags = set()

    def name(self):
        return self._name

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value
        self.sets += 1

    def fromUserText(self, text):
        self.setValue(text)

    def setValues(self, values):
        self._values = list(values)

    def values(self):
        return list(getattr(self, "_values", []))

    def setFlag(self, flag):
        self.flags.add(flag)

    def clearFlag(self, flag):
        self.flags.discard(flag)

    def execute(self):
        self.sets += 1

    def setEnabled(self, *_):
        pass

    def setVisible(self, *_):
        pass

    def setTooltip(self, *_):
        pass


# Knobs on an sgWrite group that are links to the inner Write1 in the gizmo
LINKED_TO_WRITE1 = ("file", "channels", "Render")


class Node(object):
    def __init__(self, env, node_class, name):
        self.env = env
        self._class = node_class
        self._name = name
        self._x = 0
        self._y = 0
        self._inputs = {}
        self._knobs = {}
        self.children = {}
        self.selected = False
        self.deleted = False

    # identity / layout
    def Class(self):
        return self._class

    def name(self):
        return self._name

    def setName(self, name):
        base, n = name, 1
        taken = set(x.name() for x in self.env.nodes if x is not self)
        while name in taken:
            n += 1
            name = "%s%d" % (base, n)
        self._name = name

    def xpos(self):
        return self._x

    def ypos(self):
        return self._y

    def setXYpos(self, x, y):
        self._x, self._y = x, y

    def setSelected(self, state):
        self.selected = state

    # wiring
    def input(self, index):
        return self._inputs.get(index)

    def setInput(self, index, node):
        if node is None:
            self._inputs.pop(index, None)
        else:
            self._inputs[index] = node
        return True

    def dependencies(self, what=None):
        return list(self._inputs.values())

    def dependent(self, what=None, force=True):
        return [n for n in self.env.nodes if self in n._inputs.values()]

    def channels(self):
        return list(getattr(self, "channel_names", ["rgba.red", "rgba.green", "rgba.blue"]))

    # knobs
    def knob(self, name):
        if name == "Render" and self._class == "Write":  # Write's render button
            return self._knobs.setdefault(name, Knob(name))
        if name in LINKED_TO_WRITE1 and "Write1" in self.children:
            return self.children["Write1"]._knobs.setdefault(name, Knob(name))
        return self._knobs.get(name)

    def __getitem__(self, name):
        if name in LINKED_TO_WRITE1 and "Write1" in self.children:
            return self.children["Write1"].__getitem__(name)
        if name not in self._knobs:
            self._knobs[name] = Knob(name)
        return self._knobs[name]

    def knobs(self):
        return dict(self._knobs)

    def addKnob(self, knob):
        self._knobs[knob.name()] = knob

    # group / root context
    def __enter__(self):
        self.env.stack.append(self)
        return self

    def __exit__(self, *exc):
        self.env.stack.pop()
        return False


class RootNode(Node):
    def __init__(self, env):
        Node.__init__(self, env, "Root", "root")
        self.script_path = "Root"
        self._knobs["fps"] = Knob("fps", 24.0)

    def name(self):
        return self.script_path


class Env(object):
    def __init__(self):
        self.nodes = []
        self.stack = []
        self.root = RootNode(self)
        self.selection_order = []
        self.messages = []
        self.choices = []  # every nuke.choice() prompt shown (its options)
        self.choice_answer = 0  # index the artist picks (-1 = cancel)
        self.callbacks = []
        self.deferred = []
        self.this_knob = None

    # nodes
    def add(self, node_class, name, x=0, y=0, inputs=None, **attrs):
        node = Node(self, node_class, name)
        node.setXYpos(x, y)
        for index, upstream in enumerate(inputs or []):
            node.setInput(index, upstream)
        for key, value in attrs.items():
            setattr(node, key, value)
        self.nodes.append(node)
        return node

    def delete(self, node):
        node.deleted = True
        self.nodes.remove(node)
        for other in self.nodes:
            for index, upstream in list(other._inputs.items()):
                if upstream is node:
                    other._inputs.pop(index)


def install(monkeypatch, settings=None):
    """
    Installs fake `nuke` / `sgtk` modules plus a stub tk_nuke_writenode
    package, imports the real handler and returns (env, handler_module, holder).
    """
    env = Env()
    nuke = types.ModuleType("nuke")

    nuke.GUI = True
    nuke.INVISIBLE = 0x1
    nuke.INPUTS = 1
    nuke.HIDDEN_INPUTS = 2
    nuke.root = lambda: env.root

    def to_node(name):
        if env.stack and env.stack[-1] is not env.root and env.stack[-1].children:
            return env.stack[-1].children.get(name)
        for node in env.nodes:
            if node.name() == name:
                return node
        return None

    def all_nodes(node_class=None, recurseGroups=False):
        return [n for n in env.nodes if node_class is None or n.Class() == node_class]

    def create_node(node_class, *args, **kwargs):
        node = env.add(node_class, node_class + "1")
        node.setName(node_class + "1")
        # sgWrite is a gizmo: a Group with an inner Write1 and control knobs
        if node_class == "sgWrite":
            node._class = "Group"
            node._knobs["isShotGridWriteNode"] = Knob("isShotGridWriteNode")
            node.children["Write1"] = Node(env, "Write", "Write1")
        # like the real createNode: connect to the selected node, select it
        selected = [n for n in env.nodes if n.selected and n is not node]
        if selected and node_class not in ("Read",):
            node.setInput(0, selected[0])
        for other in env.nodes:
            other.selected = False
        node.selected = True
        return node

    nuke.toNode = to_node
    nuke.allNodes = all_nodes
    nuke.createNode = create_node
    nuke.selectedNodes = lambda: [n for n in env.nodes if n.selected]
    nuke.STARTLINE = 0x2
    nuke.Tab_Knob = lambda name, label="": Knob(name)
    nuke.String_Knob = lambda name, label="", value="": Knob(name, value)
    nuke.Text_Knob = lambda name, label="", text="": Knob(name, text)

    def enumeration_knob(name, label="", values=()):
        knob = Knob(name, list(values)[0] if values else None)
        knob.setValues(values)
        return knob

    def pyscript_knob(name, label="", command=""):
        knob = Knob(name, None)
        knob.command = command
        return knob

    nuke.Enumeration_Knob = enumeration_knob
    nuke.PyScript_Knob = pyscript_knob
    nuke.message = lambda text: env.messages.append(text)

    def choice(title, prompt, options, default=0):
        env.choices.append(list(options))
        return env.choice_answer

    nuke.choice = choice
    nuke.zoom = lambda *a, **k: None
    nuke.thisKnob = lambda: env.this_knob

    def execute_deferred(call, args=(), kwargs=None):
        env.deferred.append((call, args, kwargs or {}))

    nuke.executeDeferred = execute_deferred

    nuke.Undo = types.SimpleNamespace(disable=lambda: None, enable=lambda: None)

    def register(kind):
        def add(call, *a, **k):
            env.callbacks.append((kind, call, k.get("nodeClass")))

        return add

    def unregister(kind):
        def remove(call, *a, **k):
            env.callbacks[:] = [c for c in env.callbacks if not (c[0] == kind and c[1] == call)]

        return remove

    for kind in ("OnScriptLoad", "OnCreate", "OnScriptSave", "KnobChanged"):
        setattr(nuke, "add" + kind, register(kind))
        setattr(nuke, "remove" + kind, unregister(kind))

    monkeypatch.setitem(sys.modules, "nuke", nuke)

    sgtk = types.ModuleType("sgtk")
    platform = types.ModuleType("sgtk.platform")

    class Holder(object):
        pass

    holder = Holder()
    holder.engine = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project={"type": "Project", "id": 91, "name": "storm"},
            entity={"type": "Shot", "id": 5, "name": "STRM_E1_0070"},
        )
    )
    holder.app = None
    platform.current_engine = lambda: holder.engine
    platform.current_bundle = lambda: holder.app
    platform.get_logger = lambda name: types.SimpleNamespace(
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    sgtk.platform = platform
    monkeypatch.setitem(sys.modules, "sgtk", sgtk)
    monkeypatch.setitem(sys.modules, "sgtk.platform", platform)

    pkg_dir = os.path.join(os.path.dirname(__file__), "..", "python", "tk_nuke_writenode")
    pkg = types.ModuleType("tk_nuke_writenode")
    pkg.__path__ = [pkg_dir]
    monkeypatch.setitem(sys.modules, "tk_nuke_writenode", pkg)

    dialog = types.ModuleType("tk_nuke_writenode.create_dialog")
    dialog.WriteNodePanel = type("WriteNodePanel", (), {"__init__": lambda self, *a, **k: None})
    monkeypatch.setitem(sys.modules, "tk_nuke_writenode.create_dialog", dialog)

    for name in [n for n in sys.modules if n.startswith("tk_nuke_writenode.") and n != "tk_nuke_writenode.create_dialog"]:
        monkeypatch.delitem(sys.modules, name)
    import importlib

    handler_module = importlib.import_module("tk_nuke_writenode.handler")
    return env, handler_module, holder


class FakeTemplate(object):
    """Just enough of a Toolkit Template: {token} definitions, get_fields()
    by regex, apply_fields() that raises on a missing field like TankError."""

    def __init__(self, definition, root="/jobs/SlateX"):
        self.definition = definition
        self.root = root

    def _regex(self):
        pattern = re.escape(self.root + "/" + self.definition)
        seen = set()

        def group(match):
            key = match.group(1)
            if key in seen:  # a key used twice must match the same text
                return "(?P=%s)" % key
            seen.add(key)
            return "(?P<%s>[^/]+?)" % key

        pattern = re.sub(r"\\\{(\w+)\\\}", group, pattern)
        return re.compile("^" + pattern + "$")

    def get_fields(self, path):
        match = self._regex().match(path)
        if not match:
            raise Exception("path does not match template")
        fields = match.groupdict()
        if "version" in fields:
            fields["version"] = int(fields["version"])
        return fields

    def apply_fields(self, fields):
        def sub(match):
            key = match.group(1)
            if key not in fields:
                raise Exception("missing field: %s" % key)
            value = fields[key]
            if key == "SEQ":
                return "%04d"
            if key == "version":
                return "%03d" % int(value)
            return str(value)

        return self.root + "/" + re.sub(r"\{(\w+)\}", sub, self.definition)


SHOT_ROOT = "Artists/{Sequence}/{Shot}/{Step}"
TEMPLATES = {
    "template_script_work": FakeTemplate(SHOT_ROOT + "/Nuke/{Step}/{Shot}_{Step}_v{version}.nk"),
    "nuke_shot_render": FakeTemplate(
        SHOT_ROOT
        + "/Nuke/{Step}/renders/{name}/{output}/v{version}/{Shot}_{Step}_{name}_{output}_v{version}.{SEQ}.exr"
    ),
    "nuke_shot_render_pub": FakeTemplate(
        "Publish/{Sequence}/{Shot}/{Step}/Nuke/{Step}/renders/{name}/{output}/v{version}/{Shot}_{Step}_{name}_{output}_v{version}.{SEQ}.exr"
    ),
    "nuke_shot_write_movie": FakeTemplate(
        SHOT_ROOT + "/Nuke/{Step}/previews/{Shot}_{step_lower}_OS_v{version}.mov"
    ),
    "nuke_shot_write_movie_pub": FakeTemplate(
        "Publish/{Sequence}/{Shot}/{Step}/Nuke/{Step}/previews/{Shot}_{step_lower}_OS_v{version}.mov"
    ),
}

CATEGORIES = [
    {
        "category_name": "main",
        "write_nodes": [
            {
                "name": "exr (dwaa 16bit)",
                "file_type": "exr",
                "render_template": "nuke_shot_render",
                "publish_template": "nuke_shot_render_pub",
                "tile_color": 4292673791,
                "settings": {
                    "colorspace": "ACES - ACEScg",
                    "datatype": "16 bit half",
                    "channels": "auto",
                    "compression": "DWAA",
                },
            }
        ],
    },
    {
        "category_name": "prerender",
        "write_nodes": [
            {
                "name": "exr (zip 16bit)",
                "file_type": "exr",
                "render_template": "nuke_shot_render",
                "publish_template": "nuke_shot_render_pub",
                "tile_color": 2365546239,
                "settings": {
                    "colorspace": "ACES - ACEScg",
                    "datatype": "16 bit half",
                    "channels": "rgba",
                    "compression": "Zip (1 scanline)",
                },
            }
        ],
    },
    {
        "category_name": "review",
        "write_nodes": [
            {
                # A deployed-but-not-yet-updated preset: still carries the old
                # mov64_codec/mov64_quality_max H.264 values on purpose, so
                # tests can confirm __effective_settings overrides them
                # rather than only covering the case where a preset already
                # agrees with the studio codec.
                "name": "mov (review)",
                "file_type": "mov",
                "render_template": "nuke_shot_write_movie",
                "publish_template": "nuke_shot_write_movie_pub",
                "tile_color": 2003395327,
                "settings": {
                    "colorspace": "Output - Rec.709",
                    "mov64_codec": "H.264",
                    "mov64_quality_max": 3,
                    "mov64_fps": 24,
                },
            }
        ],
    },
]


class FakeShotgun(object):
    def __init__(self, pipeline=None):
        self.pipeline = pipeline
        self.calls = 0

    def find_one(self, entity_type, filters, fields=None):
        self.calls += 1
        if entity_type == "Project":
            return {"sg_color_pipeline": self.pipeline}
        return None


class FakeApp(object):
    def __init__(self, settings=None, pipeline=None, root="/jobs/SlateX"):
        self.shotgun = FakeShotgun(pipeline)
        self._settings = {
            "categories": CATEGORIES,
            "main_category_name": "main",
            "main_write_name": "main",
            "default_category": "prerender",
            "auto_provision": True,
            "auto_provision_categories": ["main", "review"],
            "auto_read_after_render": False,
            "movie_codec": "Apple ProRes 422 HQ",
        }
        self._settings.update(settings or {})
        self._templates = {
            name: FakeTemplate(t.definition, root) for name, t in TEMPLATES.items()
        }

    def get_setting(self, name):
        return self._settings[name]

    def get_template(self, name):
        return self._templates[name]

    get_template_by_name = get_template


def script_path(version, root="/jobs/SlateX"):
    return "%s/Artists/EP_1/STRM_E1_0070/Comp/Nuke/Comp/STRM_E1_0070_Comp_v%03d.nk" % (
        root,
        version,
    )
