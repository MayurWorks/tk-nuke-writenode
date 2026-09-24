# Studio additions to tk-nuke-writenode (MayurWorks fork).
#
# Pure-python helpers for on-demand write nodes. Nothing in here imports
# nuke or sgtk, so every decision (which node to hang a write off, what to
# call it, whether the input has alpha...) is unit-testable outside of
# Nuke. handler.py owns all the actual Nuke calls and only uses these
# helpers to decide things.

import re

# Node classes that are never a sensible place to attach a render.
EXCLUDED_TAIL_CLASSES = frozenset(
    ["Viewer", "BackdropNode", "StickyNote", "Write", "Root", "NoOp"]
)

# Name of the studio template's write anchor (see StudioTemplate.nk).
WRITE_ANCHOR_NAME = "writeNoOp"


def sanitize_output_name(name):
    """Output names end up in {write.output}, which is alphanumeric-only."""
    return re.sub(r"[^a-zA-Z0-9]", "", name or "")


def next_output_name(category, taken, main_category="main", main_write_name="main"):
    """
    Picks the next free output name for a write node of ``category``.

    The main category only ever has one node (named main_write_name), so
    this returns None once that name is taken. Every other category gets
    its own name first ("review") and then a counter ("review2", ...).
    """
    taken = set(taken)
    if category == main_category:
        base = sanitize_output_name(main_write_name)
        return base if base and base not in taken else None

    base = sanitize_output_name(category)
    if not base:
        return None
    if base not in taken:
        return base
    counter = 2
    while "%s%d" % (base, counter) in taken:
        counter += 1
    return "%s%d" % (base, counter)


def channels_for(channel_names):
    """
    'rgba' when the upstream image carries alpha, else 'rgb'. Used when a
    write node's configured channels setting is the special value 'auto'.
    """
    return "rgba" if "rgba.alpha" in (channel_names or []) else "rgb"


def valid_fps(fps):
    """Returns fps as a float when it is a usable frame rate, else None."""
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        return None
    return fps if fps > 0 else None


def _depends_on(node, target_name, dependencies):
    """True if ``node`` is (transitively) fed by the node named target_name."""
    seen = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.name() in seen:
            continue
        seen.add(current.name())
        if current.name() == target_name:
            return True
        stack.extend(dependencies(current))
    return False


def pick_tail(
    nodes,
    plate_name=None,
    is_write=None,
    dependents=None,
    dependencies=None,
):
    """
    Chooses the node a new write node should be connected to: the "end of
    the comp".

    Candidates are top-level nodes nothing else (bar Viewers and write
    nodes) is connected to, excluding write nodes, viewers, backdrops, sticky notes
    and NoOps. Ranking, best first:

    1. Candidates downstream of the plate Read (the actual comp chain),
    2. anything that is not a bare Read (a lone plate is the last resort),
    3. the lowest one in the DAG (largest ypos).

    Returns None if there is no candidate at all.
    """
    is_write = is_write or (lambda node: False)
    dependents = dependents or (lambda node: [])
    dependencies = dependencies or (lambda node: [])

    candidates = []
    for node in nodes:
        if node.Class() in EXCLUDED_TAIL_CLASSES or is_write(node):
            continue
        # Viewers and our own write nodes hanging off a node don't make
        # it any less the end of the comp
        if [
            d
            for d in dependents(node)
            if d.Class() != "Viewer" and not is_write(d)
        ]:
            continue
        candidates.append(node)

    if not candidates:
        return None

    def rank(node):
        downstream = (
            plate_name is not None
            and node.name() != plate_name
            and _depends_on(node, plate_name, dependencies)
        )
        return (downstream, node.Class() != "Read", node.ypos())

    return max(candidates, key=rank)
