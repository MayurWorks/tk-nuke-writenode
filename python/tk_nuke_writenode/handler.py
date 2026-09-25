# Copyright (c) 2013 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import contextlib
import copy
import os
import re
import time

import sgtk
import nuke

from . import autopilot
from .create_dialog import WriteNodePanel

# standard toolkit logger
logger = sgtk.platform.get_logger(__name__)

# Per-project write-node category overrides, keyed by the ShotGrid
# Project field sg_color_pipeline (the same field/values used by
# tk-nuke-projectsettings' COLOR_PIPELINE_PRESETS - see that app's
# handler.py for the full rationale). Added because different studio
# projects genuinely use different color pipelines and therefore need
# different write-node data-type options (colorspace/datatype/
# compression), unlike NFA's single fixed studio-wide categories: list
# in this app's own env settings.
#
# Each entry here has the exact same shape as one item of the
# "categories" app setting (see this app's info.yml / env YAML) - a
# dict with "category_name" and "write_nodes". Only categories present
# here REPLACE the static YAML list entirely for that project (not
# merged) - see __get_categories() below for why a full replacement,
# not a merge, is the correct behavior.
#
# A project with sg_color_pipeline unset, or set to a value not present
# in this table, falls through to the static "categories" app setting
# unchanged - this app's behavior for every existing project is
# identical to before this table was added.
PER_PROJECT_WRITE_CATEGORIES = {
    "aces_acescg": [
        {
            "category_name": "main",
            "write_nodes": [
                {
                    "name": "exr (dwaa 16bit)",
                    "file_type": "exr",
                    "render_template": "nuke_shot_render",
                    "publish_template": "nuke_shot_render_pub",
                    "tile_color": 2365546239,
                    "settings": {
                        "colorspace": "ACES - ACEScg",
                        "datatype": "16 bit half",
                        "channels": "rgba",
                        "compression": "DWAA",
                    },
                }
            ],
        }
    ],
    "aces_2065_1": [
        {
            "category_name": "main",
            "write_nodes": [
                {
                    "name": "exr (dwaa 16bit)",
                    "file_type": "exr",
                    "render_template": "nuke_shot_render",
                    "publish_template": "nuke_shot_render_pub",
                    "tile_color": 2365546239,
                    "settings": {
                        "colorspace": "ACES - ACES2065-1",
                        "datatype": "16 bit half",
                        "channels": "rgba",
                        "compression": "DWAA",
                    },
                }
            ],
        }
    ],
    "rec709_sdr": [
        {
            "category_name": "main",
            "write_nodes": [
                {
                    "name": "exr (zip 16bit)",
                    "file_type": "exr",
                    "render_template": "nuke_shot_render",
                    "publish_template": "nuke_shot_render_pub",
                    "tile_color": 2365546239,
                    "settings": {
                        "colorspace": "sRGB",
                        "datatype": "16 bit half",
                        "channels": "rgba",
                        "compression": "Zip",
                    },
                }
            ],
        }
    ],
}


# File types written as a single movie rather than an image sequence.
MOVIE_FILE_TYPES = ("mov", "mov64", "mp4", "ffmpeg")

# The codec of every review movie. Studio decision: Apple ProRes (422 HQ is the
# usual dailies profile). The H.264 mov Nuke wrote here before was not reliably
# viewable, so H.264 is no longer what a "review" preset gets. Set the app's
# movie_codec setting to change it; that one setting decides, like the fps
# does, so a preset that still says mov64_codec: H.264 cannot bring it back.
DEFAULT_MOVIE_CODEC = "Apple ProRes 422 HQ"

# Knobs of Nuke's mov writer that only the H.264 encoder reads. A preset that
# still carries them is not wrong, but they mean nothing to ProRes.
H264_ONLY_KNOBS = (
    "mov64_quality_max",
    "mov64_bitrate",
    "mov64_bitrate_tolerance",
    "mov64_gop_size",
    "mov64_b_frames",
)

# Review (movie) colorspace per sg_color_pipeline value. PER_PROJECT_WRITE_
# CATEGORIES only carries the image (exr) write nodes, so without this a
# project on the nuke-default OCIO config would keep asking for the ACES
# config's "Output - Rec.709", which does not exist there. Pipelines not
# listed keep whatever the static YAML says.
PIPELINE_REVIEW_COLORSPACE = {
    "aces_acescg": "Output - Rec.709",
    "aces_2065_1": "Output - Rec.709",
    "rec709_sdr": "rec709",
}

# How long a live sg_color_pipeline lookup is reused. The autopilot asks
# for the node configuration once per write node on every sync, and there
# is no reason to hit ShotGrid for each of those.
PIPELINE_CACHE_SECONDS = 60

# Prefix of the Read node auto_read_after_render keeps up to date.
AUTO_READ_PREFIX = "render_"


# A ShotGrid write node is a plain Nuke Write node - every knob is the
# artist's to change - carrying a small "NFA ShotGrid" tab of user knobs.
# (Older scripts may still hold the sgWrite gizmo group, which stores the
# same three values in knobs named output / category / dataType.)
KNOB_TAG = "isShotGridWriteNode"
KNOB_OUTPUT = "sg_output"
KNOB_CATEGORY = "sg_category"
KNOB_DATA = "sg_data"
# The render path the pipeline last wrote to the node. A "file" that no
# longer matches it was edited by hand and is left alone.
KNOB_PATH = "sg_path"

_LEGACY_KNOBS = {"output": "output", "category": "category", "data": "dataType"}

# Label the pipeline puts on the node ("main v003"); anything else in the
# label knob is the artist's and is never overwritten.
_LABEL_PATTERN = re.compile(r"^[a-zA-Z0-9]+ v\d{3}$")

_BUTTON_SCRIPT = (
    "import sgtk\n"
    "app = sgtk.platform.current_engine().apps['tk-nuke-writenode']\n"
    "app.%s(nuke.thisNode())"
)

# Cheap on purpose: knobChanged fires for every knob change on a Write
# node (including moving it), so bail out before touching sgtk.
_KNOB_CHANGED_SCRIPT = (
    "k = nuke.thisKnob()\n"
    "if k is not None and k.name() in ('sg_category', 'sg_data', 'sg_output'):\n"
    "    import sgtk\n"
    "    app = sgtk.platform.current_engine().apps['tk-nuke-writenode']\n"
    "    app.knob_changed(nuke.thisNode(), k)"
)


def _sg_value(node, key):
    """output / category / data of a ShotGrid write node (None if absent)."""
    knob = node.knob("sg_" + key)
    if knob is None:
        knob = node.knob(_LEGACY_KNOBS[key])
    return knob.value() if knob is not None else None


def _is_sg_write(node):
    return node.Class() in ("Write", "Group") and node.knob(KNOB_TAG) is not None


def _menu_text(text):
    """Lower case, single spaces: Nuke pads codec entries with two spaces
    before the four-character code ("H.264  avc1")."""
    return " ".join(str(text).lower().split())


def _is_h264(codec):
    text = _menu_text(codec)
    return "h.264" in text or "h264" in text or "avc1" in text


def _closest_menu_item(knob, wanted):
    """The entry of an enumeration knob that ``wanted`` refers to, or None.

    The presets name menu entries the way the menu reads (colorspace "ACES -
    ACEScg", codec "Apple ProRes 422 HQ"), but a menu spells its entries
    slightly differently ("Apple ProRes 422 HQ 10-bit  apch") and Nuke does
    not always complain when setValue() gets a string that is not in the
    menu - it just stays put.

    In order: the same text; the entry's four-character codec code ("apch");
    the shortest entry that contains the text. Shortest matters for ProRes:
    "Apple ProRes 422" is a prefix of the 422 HQ, LT and Proxy entries too, and
    must select plain 422, not whichever of them comes first in the menu.
    """
    try:
        items = [str(item) for item in knob.values()]
    except Exception:
        return None
    wanted = _menu_text(wanted)
    if not wanted:
        return None
    for item in items:
        if _menu_text(item) == wanted:
            return item
    if " " not in wanted and len(wanted) == 4:
        for item in items:
            words = _menu_text(item).split()
            if words and words[-1] == wanted:
                return item
    containing = [item for item in items if wanted in _menu_text(item)]
    if containing:
        return min(containing, key=lambda item: len(_menu_text(item)))
    return None


# What the "w" prompt offers: kind -> (label, a file type is one of these)
KIND_IMAGE = "image"
KIND_MOVIE = "movie"
_KIND_LABELS = {
    KIND_IMAGE: "EXR  (image sequence)",
    KIND_MOVIE: "MOV  (review movie)",
}


@contextlib.contextmanager
def _inner_write(node):
    """The Write that actually renders: the node itself, or the Write1
    inside a legacy sgWrite group."""
    if node.Class() == "Write":
        yield node
    else:
        with node:
            yield nuke.toNode("Write1")


class NukeWriteNodeHandler(object):
    """
    Main application
    """

    def __init__(self):
        self.app = sgtk.platform.current_bundle()
        self.sg = self.app.shotgun
        # {project id: (looked up at, sg_color_pipeline value)}
        self._pipeline_cache = {}

    def __get_pipeline_key(self):
        """
        The current project's sg_color_pipeline value (None if unset or if
        there is no project). Read live from ShotGrid - the same field
        tk-nuke-projectsettings' _get_color_pipeline uses, so both apps
        always agree - and cached for PIPELINE_CACHE_SECONDS.

        Raises whatever the ShotGrid query raises; __get_categories()
        turns that into a fall back to the static YAML.
        """
        engine = sgtk.platform.current_engine()
        context = engine.context
        project = context.project if context else None
        if not project:
            return None

        cached = self._pipeline_cache.get(project["id"])
        if cached and time.time() - cached[0] < PIPELINE_CACHE_SECONDS:
            return cached[1]

        result = self.sg.find_one(
            "Project", [["id", "is", project["id"]]], ["sg_color_pipeline"]
        )
        key = (result or {}).get("sg_color_pipeline") or None
        self._pipeline_cache[project["id"]] = (time.time(), key)
        return key

    def __get_categories(self):
        """
        Resolves the write-node "categories" configuration to use for
        the current project - see PER_PROJECT_WRITE_CATEGORIES above.

        A project whose sg_color_pipeline is set to a value present in
        PER_PROJECT_WRITE_CATEGORIES gets those categories in place of
        the static YAML categories *of the same name*. Static categories
        the override does not mention (e.g. "review") are kept, so a
        project on a custom colour pipeline still gets its review
        write node - previously the override replaced the whole list and
        that category silently vanished. Movie write nodes additionally
        get the pipeline's review colorspace (PIPELINE_REVIEW_COLORSPACE).

        Falls back to the static "categories" app setting unchanged if
        the field is unset, the live query fails, or the value is not in
        the table - an unconfigured project behaves exactly as before.

        Returns the same shape self.app.get_setting("categories")
        returns.
        """
        static_categories = self.app.get_setting("categories")

        try:
            pipeline_key = self.__get_pipeline_key()
        except Exception:
            logger.warning(
                "tk-nuke-writenode: live sg_color_pipeline query failed, "
                "falling back to this app's static 'categories' setting",
                exc_info=True,
            )
            return static_categories

        if not pipeline_key:
            return static_categories

        if pipeline_key not in PER_PROJECT_WRITE_CATEGORIES:
            logger.warning(
                "tk-nuke-writenode: project's sg_color_pipeline value "
                "'%s' has no entry in PER_PROJECT_WRITE_CATEGORIES, "
                "falling back to this app's static 'categories' setting",
                pipeline_key,
            )
            return static_categories

        override = PER_PROJECT_WRITE_CATEGORIES[pipeline_key]
        overridden = set(c.get("category_name") for c in override)
        categories = list(override) + [
            c
            for c in (static_categories or [])
            if c.get("category_name") not in overridden
        ]

        review_colorspace = PIPELINE_REVIEW_COLORSPACE.get(pipeline_key)
        if review_colorspace:
            categories = copy.deepcopy(categories)
            for category in categories:
                for write_node in category.get("write_nodes", []):
                    if write_node.get("file_type") in MOVIE_FILE_TYPES:
                        write_node.setdefault("settings", {})[
                            "colorspace"
                        ] = review_colorspace

        return categories

    def render_local(self, node):
        """Render the specified node.
        Will create paths and render

        Args:
            node (attribute): node to render
        """

        # Set paths for node
        prepared_write = self.__prepare_write(node)

        # If paths are set, render
        if prepared_write:
            node.knob("Render").execute()

            # Rendered without errors: keep the auto Read node in step
            self.__auto_read(node)

        # If paths hasn't been set, let user know something went wrong
        else:
            nuke.message("Something went wrong.")

    def render_farm(self, node):
        """Submit the node to render on farm.
        Will create paths and submit

        Args:
            node (attribute): node to submit to farm
        """

        # Set parameters for node before rendering
        prepared_write = self.__prepare_write(node)
        if prepared_write:

            # Using https://github.com/gillesvink/NukeDeadlineSubmission
            import deadline_submission

            # Submit node for rendering on farm
            submit = deadline_submission.DeadlineSubmission().submit(node)

            # If submitted, increment save to not touch script while rendering
            if submit:
                self.__increment_save()
        else:
            nuke.message("Something went wrong.")

    def create_writenode(self):
        """This function will use the Write Node create panel
        and set up the node correctly.

        It will check if the node already exists, and if so, it will show
        the user the already existing node.
        """

        # Get all write nodes to check if the created name exists
        write_nodes = self.get_all_write_nodes()

        # Create initial list to add all names to
        write_names = []
        for node in write_nodes:
            node = nuke.toNode(node)

            # Append name to list
            write_names.append(_sg_value(node, "output"))

        # Get all options possible for write nodes
        write_node_settings = self.__get_write_node_options()

        # Get default names
        main_category_name = self.app.get_setting("main_category_name")
        main_write_name = self.app.get_setting("main_write_name")
        default_category = self.app.get_setting("default_category")

        # Give variables to write node panel
        write_node_data = WriteNodePanel(
            default_category,
            write_node_settings,
            main_category_name,
            main_write_name,
        )

        # Set panel minimum width and height
        write_node_data.setMinimumSize(200, 190)

        # Open panel, and if user proceeded, continue
        if write_node_data.showModalDialog():

            # Get output name
            output_name = write_node_data.output_knob.value()

            # Validate name
            if not len(output_name) > 0:
                nuke.message("No name specified, please specify a name.")
                return

            # If name already exists, show message and go to node
            if output_name in write_names:
                nuke.message("Write node %s already existing." % output_name)
                self.go_to_write_node(output_name)
                return

            # Set regex for output name validation
            regex = re.compile(r"[a-zA-Z0-9]*$")

            # Validate name
            if not regex.match(output_name):
                nuke.message(
                    "Name contains illegal characters. Please only use letters "
                    "and numbers. \n[a-zA-Z0-9]"
                )
                return

            # Get category name
            category = write_node_data.category_knob.value()

            # If category is another than main category, but has the main write
            # name in it, it is not allowed because only one "main" node is
            # allowed to exist, in the "main" category
            if category != main_category_name:
                if output_name == main_write_name:
                    nuke.message(
                        "Name %s only allowed on %s category."
                        % (output_name, main_category_name)
                    )
                    return

            write_data = write_node_data.data_knob.value()

            self.__create_write(
                write_node_settings, category, output_name, write_data
            )

    def knob_changed(self, node, knob):
        """Function called when the output, category or data knob changes
        on a ShotGrid write node - the artist picking a different preset.

        A new category swaps the list of data presets; a new preset
        re-applies that preset's settings (file type, colorspace,
        compression, ...) and recalculates the render path, so switching
        main -> review changes both what is written and where.

        Args:
            node (attribute): node to process
            knob (attribute): knob that has changed
        """
        if getattr(self, "_busy", False):
            return
        name = knob.name()
        if name not in (KNOB_CATEGORY, KNOB_DATA, KNOB_OUTPUT, "dataType"):
            return

        self._busy = True
        try:
            if name == KNOB_CATEGORY:
                options = self.__get_write_node_options()
                presets = options.get(node[KNOB_CATEGORY].value()) or []
                if presets:
                    node[KNOB_DATA].setValues(presets)
                    node[KNOB_DATA].setValue(presets[0])

            if name == KNOB_OUTPUT:
                # the output name ends up in the file name: letters and
                # numbers only. Then only the path depends on it.
                value = node[KNOB_OUTPUT].value()
                clean = autopilot.sanitize_output_name(value)
                if clean and clean != value:
                    node[KNOB_OUTPUT].setValue(clean)
                self.__prepare_write(node, quiet=True, create_dirs=False)
            else:
                self.__prepare_write(node, quiet=True, mode="full")
            logger.debug("Updated node settings")
        finally:
            self._busy = False

    def apply_write_settings(self, node):
        """The node's "apply studio settings" button: puts every setting of
        the node's category / preset back to what the pipeline specifies
        and recalculates the render path (an edited path included)."""
        if not self.__prepare_write(node, mode="full", create_dirs=False):
            nuke.message("Could not apply the studio settings to this node.")

    def read_from_selected(self):
        """Create read node from the selected node"""

        try:
            # Select current selected node
            node = nuke.selectedNode()

            # Create read from write with specified node
            self.read_from_write(node)

        # If something went wrong, e.g no node selected, let user know
        except Exception as error:
            nuke.message(str(error))

    def read_from_write(self, node, name=None, reuse=False, inpanel=True):
        """Create read node from node.

        Will add a read node with the latest render underneath the node.

        Args:
            node (attribute): node to create read node from
            name (str, optional): name for the Read node
            reuse (bool, optional): with a name, update the existing Read
                node of that name instead of creating another one
            inpanel (bool, optional): open the new node's properties panel

        Returns:
            attribute: the Read node, or None if there was nothing to read
        """

        # Make sure we are in nuke root level
        with nuke.root():

            # Get render path
            render_path = node["file"].value()

            if render_path == "":
                nuke.message(
                    "This write node has not rendered yet, please render"
                    " before create a read from this write node."
                )
                return None

            # Check for publish status
            is_published = self.get_published_status(node)

            # If it is published, use publish path
            if is_published:
                render_path = self.__get_published_path(node, render_path)

            # Get directory for render
            render_directory = os.path.dirname(render_path)

            # Get frame sequences from directory, we will use this function
            # to get the first and last frame to set the read node
            frame_sequences = self.__get_frame_sequences(render_directory)

            # Iterate trough all found frame sequences
            for frame_sequence in frame_sequences:
                sequence_path = frame_sequence[0].replace(os.sep, "/")

                # If sequence path matches render path we know this is the one
                if sequence_path == render_path:

                    read_node = None
                    if reuse and name:
                        read_node = nuke.toNode(name)
                        if read_node is not None and read_node.Class() != "Read":
                            read_node = None

                    if read_node is None:
                        # Create read node
                        read_node = nuke.createNode("Read", inpanel=inpanel)
                        if name:
                            read_node.setName(name)
                        # Set position (only for a new node, an existing
                        # one stays wherever the artist put it)
                        read_node["xpos"].setValue(node.xpos())
                        read_node["ypos"].setValue(node.ypos() + 50)

                    # Set path
                    read_node["file"].fromUserText(render_path)

                    # Set colorspace
                    read_node["colorspace"].setValue(self.get_colorspace(node))

                    # Set parameters
                    start_frame = int(min(frame_sequence[1]))
                    last_frame = int(max(frame_sequence[1]))

                    read_node["first"].setValue(start_frame)
                    read_node["origfirst"].setValue(start_frame)
                    read_node["last"].setValue(last_frame)
                    read_node["origlast"].setValue(last_frame)

                    return read_node

        return None

    def __auto_read(self, node):
        """
        After a local render, creates - or refreshes - a Read node named
        render_<output> pointing at what was just written, so the result
        is on the DAG without anybody clicking "create read from write".
        Only for image sequences (a movie is reviewed, not read back
        in), only if the auto_read_after_render setting is on, and never
        allowed to raise: the render itself already succeeded.
        """
        try:
            if not self.__setting_enabled("auto_read_after_render"):
                return None
            with _inner_write(node) as write_node:
                file_type = write_node["file_type"].value()
            if file_type in MOVIE_FILE_TYPES:
                return None
            return self.read_from_write(
                node,
                name=AUTO_READ_PREFIX + _sg_value(node, "output"),
                reuse=True,
                inpanel=False,
            )
        except Exception:
            logger.warning(
                "tk-nuke-writenode: auto Read after render failed",
                exc_info=True,
            )
            return None

    def convert_placeholder_nodes(self):
        """Search existing placeholder nodes, and creates write nodes
        accordingly with the correct settings.

        This function can be used in templates to set write node types,
        and create correct write nodes while loading script for first time"""

        # Filter all nodes to scan for ModifyMetaData nodes
        for placeholder_node in nuke.allNodes("ModifyMetaData"):

            # If placeholder node starts with ShotGridWriteNodePlaceholder, we
            # know this is the node we want to replace with a
            # correct write node
            if placeholder_node.name().startswith(
                "ShotGridWriteNodePlaceholder"
            ):
                # Get write node settings
                write_node_settings = self.__get_write_node_options()

                # Get provided data from metadata node
                metadata = placeholder_node.metadata()
                category = metadata.get("category")
                output_name = metadata.get("output")
                data_type = metadata.get("data_type")

                # Get position and input data to replace node
                placeholder_xpos = placeholder_node.xpos()
                placeholder_ypos = placeholder_node.ypos()
                placeholder_input = placeholder_node.input(0).name()

                # Delete the old node
                nuke.delete(placeholder_node)

                # Create write node
                write_node = self.__create_write(
                    write_node_settings, category, output_name, data_type
                )

                # Set position data
                write_node["xpos"].setValue(placeholder_xpos)
                write_node["ypos"].setValue(placeholder_ypos)
                write_node.setInput(0, nuke.toNode(placeholder_input))

            else:
                # If no nodes have been found, skip conversion
                logger.debug(
                    "No ShotGrid Write Node placeholder node found, "
                    "skipping conversion."
                )

    def add_callbacks(self):
        """Adds callbacks on script load, save and rename"""
        nuke.addOnScriptLoad(self.convert_placeholder_nodes, nodeClass="Root")

        # Write nodes are only ever created on demand (the "w" hotkey), but
        # once one exists its render path should follow the script through
        # version-ups and save-as: never render v002 into the v001 folder.
        if self.__setting_enabled("follow_script_version"):
            nuke.addOnScriptSave(self._on_script_save, nodeClass="Root")
            nuke.addKnobChanged(self._on_root_knob_changed, nodeClass="Root")

    def remove_callbacks(self):
        """Removes callbacks on destroy"""
        nuke.removeOnScriptLoad(
            self.convert_placeholder_nodes, nodeClass="Root"
        )
        for remove, callback in (
            (nuke.removeOnScriptSave, self._on_script_save),
            (nuke.removeKnobChanged, self._on_root_knob_changed),
        ):
            try:
                remove(callback, nodeClass="Root")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # On-demand write nodes
    #
    # Nothing here creates a node by itself - not on open, not on save.
    # A write node appears when the artist asks for one ("w"), fully set
    # up for this shot and project. After that it is an ordinary Write
    # node the artist owns; the pipeline only keeps its render path on
    # the current script version - and only while the path is still the
    # one the pipeline wrote.
    # ------------------------------------------------------------------

    def __setting_enabled(self, name):
        try:
            return bool(self.app.get_setting(name))
        except Exception:
            return False

    @contextlib.contextmanager
    def __quiet_undo(self):
        """Background path updates must not end up on the undo stack."""
        disabled = False
        try:
            nuke.Undo.disable()
            disabled = True
        except Exception:
            pass
        try:
            yield
        finally:
            if disabled:
                try:
                    nuke.Undo.enable()
                except Exception:
                    pass

    def _script_fields(self, script_path=None):
        """Fields of the script, or None if it is not a valid work file
        (unsaved, or saved somewhere outside the work template)."""
        path = script_path or nuke.root().name()
        if not path or path == "Root":
            return None
        try:
            template = self.app.get_template("template_script_work")
            return template.get_fields(path)
        except Exception:
            return None

    def _find_tail(self):
        """The node a new write should be connected to. The studio
        template's writeNoOp anchor wins if it is still around; otherwise
        the end of the comp chain (see autopilot.pick_tail)."""
        anchor = nuke.toNode(autopilot.WRITE_ANCHOR_NAME)
        if anchor is not None:
            return anchor

        plate_name = None
        try:
            entity = sgtk.platform.current_engine().context.entity
            if entity and entity.get("name"):
                plate_name = "plate_%s" % entity["name"]
        except Exception:
            pass

        both = nuke.INPUTS | nuke.HIDDEN_INPUTS
        return autopilot.pick_tail(
            nuke.allNodes(),
            plate_name=plate_name,
            is_write=_is_sg_write,
            dependents=lambda node: node.dependent(both, False),
            dependencies=lambda node: node.dependencies(both),
        )

    def _existing_write_info(self):
        """[(category, output)] of the write nodes already in the script."""
        return [
            (_sg_value(node, "category"), _sg_value(node, "output"))
            for node in map(nuke.toNode, self.get_all_write_nodes())
        ]

    def sync_all(self, script_path=None):
        """
        Re-points every ShotGrid write node at the render path of the
        script's current version. Only the path (and the version label)
        moves: nothing the artist tuned on the node is touched, and a
        path that was edited by hand is left exactly as it is.

        Returns:
            int: number of write nodes looked at
        """
        if not self.__setting_enabled("follow_script_version"):
            return 0
        if self._script_fields(script_path) is None:
            return 0

        synced = 0
        for name in self.get_all_write_nodes():
            node = nuke.toNode(name)
            try:
                if self.__prepare_write(
                    node,
                    script_path=script_path,
                    create_dirs=False,
                    quiet=True,
                ):
                    synced += 1
            except Exception:
                logger.warning(
                    "tk-nuke-writenode: could not sync write node %s" % name,
                    exc_info=True,
                )
        return synced

    def run_sync(self, reason="", script_path=None):
        """sync_all() as a background job: quiet, never raises."""
        try:
            with self.__quiet_undo():
                self.sync_all(script_path)
        except Exception:
            logger.warning(
                "tk-nuke-writenode: path sync failed (%s)" % reason,
                exc_info=True,
            )

    def on_before_save(self, script_path=None):
        """Called by the workfiles2 scene-operation hook right before a
        save / save-as is written, with the path about to be saved to, so
        the file on disk already carries the right render paths."""
        self.run_sync("before-save", script_path=script_path)

    def schedule_sync(self, reason=""):
        """Runs run_sync() once the current callback chain has finished.
        GUI sessions only: a farm / headless render must never edit the
        script it was asked to render."""
        if not getattr(nuke, "GUI", False):
            return
        try:
            nuke.executeDeferred(self.run_sync, (reason,))
        except Exception:
            logger.warning(
                "tk-nuke-writenode: could not schedule path sync",
                exc_info=True,
            )

    def _on_script_save(self):
        if not getattr(nuke, "GUI", False):
            return
        self.run_sync("script-save")

    def _on_root_knob_changed(self):
        # Save As / version-up changes the script's name, and with it the
        # version every render path is built from.
        try:
            knob = nuke.thisKnob()
            if knob is not None and knob.name() == "name":
                self.schedule_sync("script-renamed")
        except Exception:
            pass

    def __category_kinds(self):
        """{category: KIND_IMAGE | KIND_MOVIE}, from each category's first
        write node preset."""
        kinds = {}
        for category in self.__get_categories() or []:
            write_nodes = category.get("write_nodes") or []
            if write_nodes:
                file_type = write_nodes[0].get("file_type")
                kinds[category.get("category_name")] = (
                    KIND_MOVIE if file_type in MOVIE_FILE_TYPES else KIND_IMAGE
                )
        return kinds

    @staticmethod
    def __ask_kind(kinds):
        """The "w" prompt: EXR or MOV? Returns the kind, or None if the
        artist cancelled."""
        labels = [_KIND_LABELS[kind] for kind in kinds]
        try:
            index = nuke.choice(
                "NFA ShotGrid Write Node",
                "Which type of write node do you want?",
                labels,
                0,
            )
        except Exception:
            return None
        if index is None or index < 0 or index >= len(kinds):
            return None
        return kinds[index]

    def create_writenode_auto(self, kind=None):
        """
        The "w" hotkey: asks which type of write node - EXR or MOV - then
        creates it immediately, already set up for this shot and project:
        preset, colorspace, file type and the versioned render path. No
        naming, no further dialog.

        EXR: the configured default category (falling back to the first
        one with a free name) and the next free name, so pressing w
        repeatedly gives main, prerender, prerender2, ...
        MOV: the review category. The movie's file name has no per-node
        part (STRM_E1_0070_cmp_OS_v003.mov), so there is one per script
        version: if one exists already it is selected instead of
        creating a second that would overwrite it.

        It is connected to the selected node or, with nothing selected,
        the end of the comp. The old dialog is still available as
        "custom...".

        Args:
            kind (str, optional): KIND_IMAGE or KIND_MOVIE to skip the
                prompt

        Returns:
            attribute: the new (or, for a second MOV, the existing) node
        """
        options = self.__get_write_node_options()
        if not options:
            nuke.message("No write node categories are configured.")
            return None

        category_kinds = self.__category_kinds()
        available = [
            k for k in (KIND_IMAGE, KIND_MOVIE) if k in category_kinds.values()
        ]
        if not available:
            nuke.message("No write node categories are configured.")
            return None
        if kind is None:
            kind = available[0] if len(available) == 1 else self.__ask_kind(available)
            if kind is None:
                return None  # cancelled
        # the node keeps every category in its dropdown (so an EXR node can
        # still be switched to review), but a new one is picked from this kind
        all_options = options
        options = dict(
            (c, presets) for c, presets in all_options.items() if category_kinds.get(c) == kind
        )
        if not options:
            nuke.message("No %s write node is configured." % _KIND_LABELS[kind])
            return None

        if kind == KIND_MOVIE:
            for name in self.get_all_write_nodes():
                existing = nuke.toNode(name)
                if category_kinds.get(_sg_value(existing, "category")) == KIND_MOVIE:
                    nuke.message(
                        "This script already has a MOV write node (%s): its "
                        "file name is per version, a second one would "
                        "overwrite it." % existing.name()
                    )
                    for node in nuke.selectedNodes():
                        node.setSelected(False)
                    existing.setSelected(True)
                    self.go_to_write_node(_sg_value(existing, "output"))
                    return existing

        main_category = self.app.get_setting("main_category_name")
        main_write_name = self.app.get_setting("main_write_name")
        default = self.app.get_setting("default_category")

        taken = set(output for _, output in self._existing_write_info())
        order = ([default] if default in options else []) + [
            c for c in options if c != default
        ]
        for category in order:
            output = autopilot.next_output_name(
                category, taken, main_category, main_write_name
            )
            if output and options.get(category):
                break
        else:
            nuke.message("Every write node category is already in use.")
            return None

        selected = [n for n in nuke.selectedNodes() if not _is_sg_write(n)]
        if len(selected) == 1 and selected[0].Class() != "Viewer":
            upstream = selected[0]
        else:
            upstream = self._find_tail()

        xy = (
            (upstream.xpos(), upstream.ypos() + 100)
            if upstream is not None
            else None
        )

        with nuke.root():
            return self.__create_write(
                all_options,
                category,
                output,
                options[category][0],
                input_node=upstream,
                auto=True,
                xy=xy,
            )

    def update_read_nodes(self):
        """Updates all read nodes to use published path instead
        of work path
        """
        # Get all write nodes, to retrieve rendered file paths
        write_nodes = self.get_all_write_nodes()

        # Build dictionary containing both write node and file path
        image_sequences = {}

        # Iterate trough all write nodes
        for write_node in write_nodes:

            # We only got a name, so we need to get the attributes
            write_node = nuke.toNode(write_node)

            # Get render path
            render_path = write_node["file"].value()

            # Append write node to the rendered path
            image_sequences[render_path] = write_node

        # Filter for all read nodes
        all_nodes = nuke.allNodes("Read")

        # Iterate trough all read nodes
        for node in all_nodes:

            # If path in read node is in the directory we just created
            # set publish path
            read_path = node["file"].value()
            if read_path in image_sequences.keys():

                # Calculate publish path
                write_node = image_sequences.get(read_path)
                published_path = self.__get_published_path(
                    write_node, read_path
                )

                # Set publish path to read node
                node["file"].setValue(published_path)

    @staticmethod
    def get_all_write_nodes():
        """Get all ShotGrid write nodes in list

        Returns:
            list: write node names in current script
        """
        # Plain Write nodes carry the isShotGridWriteNode tag knob (so do
        # the sgWrite groups older scripts may still contain)
        return [
            node.name()
            for node in nuke.allNodes()
            if _is_sg_write(node)
        ]

    @staticmethod
    def go_to_write_node(output_name):
        """Will move the DAG towards the write node using
        the specified output_name

        Args:
            output_name (str): output name of the node to go to
        """
        for node in nuke.allNodes():
            if _is_sg_write(node) and _sg_value(node, "output") == output_name:
                # Position DAG to position of node
                nuke.zoom(3, [node.xpos(), node.ypos()])

    def get_node_render_template(self, node):
        """Get  render template used by the specified node

        Args:
            node (attribute): node to get render template used

        Returns:
            attribute: render template from templates.yml
        """

        # Get configuration for node
        configuration = self.__get_node_settings(node)

        # Get render template
        render_template = configuration.get("render_template")

        # Find template in templates.yml
        render_template = self.app.get_template_by_name(render_template)

        return render_template

    def get_node_publish_template(self, node):
        """Get publish template used by the specified node

        Args:
            node (attribute): node to get publish template used

        Returns:
            attribute: publish template from templates.yml
        """
        # Get configuration for node
        configuration = self.__get_node_settings(node)

        # Get publish template
        publish_template = configuration.get("publish_template")

        # Find template in templates.yml
        publish_template = self.app.get_template_by_name(publish_template)

        return publish_template

    def get_published_status(self, node):
        """This function will check on ShotGrid if there is a publish with
        exactly the same name on the project.

        Args:
            node (attribute): node to retrieve publish status

        Returns:
            bool: If there is a publish existing it will return
            "True", otherwise return a "False" value
        """

        sg = self.sg

        # Get file path for node
        file_name = node["file"].value()

        # Get file name only
        file_name = os.path.basename(file_name)

        # Get current project ID
        current_engine = sgtk.platform.current_engine()
        current_context = current_engine.context
        project_id = current_context.project["id"]

        # Create the filter to search on ShotGrid for
        # publishes with the same file name
        filters = [
            ["project", "is", {"type": "Project", "id": project_id}],
            ["code", "is", file_name],
        ]

        # Search on ShotGrid
        published_file = sg.find_one("PublishedFile", filters)

        # If there is no publish, it will return a None value.
        # So set the variable is_published to "False"
        if published_file is None:
            is_published = False

        # If the value is not None, there is a publish with the same name.
        # So set the variable is_published to "True"
        else:
            is_published = True

        return is_published

    @staticmethod
    def get_colorspace(node):
        """Get colorspace node is rendering

        Args:
            node (attribute): node to get colorspace

        Returns:
            str: colorspace
        """
        with _inner_write(node) as write_node:
            # Get colorspace knob value
            return write_node["colorspace"].value()

    def __create_write(
        self,
        write_node_settings,
        category,
        output_name,
        data_type,
        input_node=None,
        auto=False,
        xy=None,
        script_path=None,
    ):
        """Create a write node using the specified settings

        The result is a plain Nuke Write node with an "NFA ShotGrid" tab
        added - all of Write's own knobs stay in the artist's hands.

        Args:
            write_node_settings (dict): containing all parameters to setup node
            category (str): category user has chosen to setup node
            output_name (str): output name to render
            data_type (str): datatype (preset) to use
            input_node (attribute, optional): node to connect the write to
            auto (bool, optional): created without a dialog: no properties
                panel, nothing selected/connected by accident, no popups
            xy (tuple, optional): position (x, y) in the DAG
            script_path (str, optional): script path to derive the render
                path from, if not the current one

        Returns:
            attribute: created write node
        """

        if auto:
            # createNode() connects whatever is selected - not wanted here
            for selected in nuke.selectedNodes():
                selected.setSelected(False)

        created_write = nuke.createNode("Write", inpanel=not auto)

        if input_node is not None:
            created_write.setInput(0, input_node)
        if xy is not None:
            created_write.setXYpos(int(xy[0]), int(xy[1]))

        self.__add_sg_knobs(
            created_write, write_node_settings, category, output_name, data_type
        )

        # Preset settings + the render path, set immediately so the node
        # shows where it will render the moment it exists. No folders are
        # made yet: Nuke creates them when something renders.
        self.__prepare_write(
            created_write,
            script_path=script_path,
            create_dirs=False,
            quiet=auto,
            mode="full",
        )

        # Only now listen for preset changes, so none of the above can
        # trigger it
        created_write["knobChanged"].setValue(_KNOB_CHANGED_SCRIPT)

        return created_write

    @staticmethod
    def __add_sg_knobs(node, options, category, output_name, data_type):
        """The "NFA ShotGrid" tab: what this write is (output, category,
        preset) and the studio buttons (render, farm, read, apply)."""
        node.addKnob(nuke.Tab_Knob("sg_tab", "NFA ShotGrid"))

        node.addKnob(nuke.String_Knob(KNOB_OUTPUT, "output", output_name))
        node.addKnob(
            nuke.Enumeration_Knob(KNOB_CATEGORY, "category", list(options))
        )
        node[KNOB_CATEGORY].setValue(category)
        node.addKnob(
            nuke.Enumeration_Knob(
                KNOB_DATA, "data", list(options.get(category) or [])
            )
        )
        node[KNOB_DATA].setValue(data_type)

        buttons = (
            ("sg_render_local", "render", "render_local", True),
            ("sg_render_farm", "render on farm", "render_farm", False),
            ("sg_read", "create read from write", "read_from_write", True),
            ("sg_apply", "apply studio settings", "apply_write_settings", False),
        )
        for name, label, method, new_line in buttons:
            button = nuke.PyScript_Knob(name, label, _BUTTON_SCRIPT % method)
            node.addKnob(button)
            if not new_line:
                try:
                    node[name].clearFlag(nuke.STARTLINE)
                except Exception:
                    pass

        # Hidden bookkeeping: the last path the pipeline set, and the tag
        # that marks this Write as a ShotGrid write node
        node.addKnob(nuke.String_Knob(KNOB_PATH, "path"))
        node[KNOB_PATH].setFlag(nuke.INVISIBLE)
        node.addKnob(nuke.Text_Knob(KNOB_TAG, ""))
        node[KNOB_TAG].setFlag(nuke.INVISIBLE)

    @staticmethod
    def __input_channels(node):
        """Channel names of whatever feeds ``node`` (empty if unconnected)."""
        try:
            upstream = node.input(0)
            return list(upstream.channels()) if upstream is not None else []
        except Exception:
            return []

    def __get_write_node_options(self):
        """This function will build a dictionary containing
        the category name and write node names

        Returns:
            dict: category name and write node names

            For example: {
            "main": ["exr (dwaa 16bit)"],
            "prerender": [
                "exr (dwaa 16bit)",
                "exr (zip 16bit)",
                "exr (zip 32bit)",
            ],
            "mattepainting": ["tiff (deflate 16 bit)"],
        }
        """

        # Get categories from settings - per-project if sg_color_pipeline
        # is set and recognised, otherwise this app's static YAML list
        # (see __get_categories).
        categories = self.__get_categories()

        # Create initial dictionary to add settings to
        write_node_settings = {}
        for category in categories:

            # Get category name
            category_name = category.get("category_name")

            # Get write node names
            write_nodes = category.get("write_nodes")
            write_node_names = []
            for write_node in write_nodes:
                write_node_name = write_node.get("name")
                write_node_names.append(write_node_name)

            # Add list with names to category
            write_node_settings[category_name] = write_node_names

        return write_node_settings

    def __get_latest_version(self, node):
        """This function will check on ShotGrid if there is a publish with
        exactly the same name on the project.

        Args:
            node (attribute): node to retrieve publish status

        Returns:
            bool: If there is a publish existing it will return
            "True", otherwise return a "False" value
        """

        sg = self.sg

        # Get file path for node
        file_name = node["file"].value()

        # Get file name only
        file_name = os.path.basename(file_name)

        # Get current project ID
        current_engine = sgtk.platform.current_engine()
        current_context = current_engine.context
        project_id = current_context.project["id"]

        # Create the filter to search on ShotGrid for
        # publishes with the same file name
        filters = [
            ["project", "is", {"type": "Project", "id": project_id}],
            ["code", "is", file_name],
        ]

        # Search on ShotGrid
        published_file = sg.find_one("PublishedFile", filters)

        # If there is no publish, it will return a None value.
        # So set the variable is_published to "False"
        if published_file is None:
            is_published = False

        # If the value is not None, there is a publish with the same name.
        # So set the variable is_published to "True"
        else:
            is_published = True

        return is_published

    def __get_node_settings(self, node):
        """This function will go trough the dictionary to get the correct
        configuration dictionary matching the settings of the node

        Args:
            node (attribute): node to setup

        Returns:
            dict: containing all settings to set write node

            For example: {
            "name": "exr (dwaa 16bit)",
            "file_type": "exr",
            "render_template": "nuke_shot_render_work",
            "publish_template": "nuke_shot_render_pub",
            "tile_color": 2365546239,
            "settings": {
                "colorspace": "scene_linear",
                "datatype": "16 bit half",
                "channels": "rgba",
                "compression": "DWAA",
            },
        }
        """

        # Get required information to get settings
        write_category = _sg_value(node, "category")
        data_type = _sg_value(node, "data")

        categories = self.__get_categories()
        for category in categories:

            # If category name matches our name, it is the category
            if category.get("category_name") == write_category:

                # Search trough all possible write nodes
                for write_node in category.get("write_nodes"):

                    # If write node matches our data type, we need
                    # these settings
                    if write_node.get("name") == data_type:

                        return write_node

    def __resolve_render_path(self, node, configuration, script_path=None):
        """Calculate write path using template provided in configuration

        Args:
            node (attribute): node to calculate path
            configuration (dict): configuration containing template
            script_path (str, optional): script path to take the fields
                from instead of the current script (a save-as target)

        Returns:
            tuple: (file path for rendering, the fields it was built from),
            or (None, None) if the script isn't a valid work file (unsaved,
            or saved outside the work template) and so has no render
            location yet.
        """

        # Get render template from settings
        render_template = configuration.get("render_template")

        # Search for render template in templates.yml
        render_template = self.app.get_template_by_name(render_template)

        # Get script template
        script_template = self.app.get_template("template_script_work")

        # Get values for fields
        current_file = script_path or nuke.root().name()

        # Get fields already set by script path
        try:
            fields = dict(script_template.get_fields(current_file))
        except Exception:
            logger.debug(
                "tk-nuke-writenode: '%s' is not a valid work file, no "
                "render path yet" % current_file
            )
            return None, None

        output = _sg_value(node, "output")
        fields["SEQ"] = "FORMAT: %d"
        fields["output"] = output

        # The work template ({Shot}_{Step}_v{version}.nk) has no {name},
        # but every render template asks for one, so resolving would
        # raise TankError for a missing field. The output name is the
        # natural value: it is what tells two write nodes apart.
        fields.setdefault("name", output)

        # Some file names want the step in lower case (..._cmp_OS_v003.mov)
        # while its folder keeps the shotgrid short name (CMP): templates
        # get both, Step and step_lower.
        if fields.get("Step"):
            fields.setdefault("step_lower", str(fields["Step"]).lower())

        # Calculate path
        render_path = render_template.apply_fields(fields).replace(os.sep, "/")

        return render_path, fields

    def __calculate_path(self, node, configuration, script_path=None):
        """Render path for node, or None - see __resolve_render_path()"""
        return self.__resolve_render_path(node, configuration, script_path)[0]

    @staticmethod
    def __set_knob(target, name, value):
        """setValue() only when the value differs, and never raises. Keeps a
        no-op sync from dirtying the script. A setting that ends up not
        applied is logged as a warning (Toolkit log), not swallowed."""
        if value is None:
            return
        try:
            knob = target[name]
            if str(knob.value()) == str(value):
                return
            try:
                knob.setValue(value)
            except Exception:
                pass

            # A menu entry spelled differently from the preset
            if isinstance(value, str) and str(knob.value()) != value:
                match = _closest_menu_item(knob, value)
                if match is not None and match != str(knob.value()):
                    knob.setValue(match)

            if isinstance(value, str) and str(knob.value()) != value:
                if _closest_menu_item(knob, value) != str(knob.value()):
                    logger.warning(
                        "tk-nuke-writenode: could not apply '%s' to the knob "
                        "%s (it is '%s')" % (value, name, knob.value())
                    )
        except Exception as error:
            logger.warning(
                "tk-nuke-writenode: could not apply %s to the knob %s, "
                "because %s" % (value, name, str(error))
            )

    def __get_movie_codec(self):
        """The app's "movie_codec" setting, or DEFAULT_MOVIE_CODEC if unset
        (older configs deployed before this setting existed)."""
        try:
            return self.app.get_setting("movie_codec") or DEFAULT_MOVIE_CODEC
        except Exception:
            return DEFAULT_MOVIE_CODEC

    def __effective_settings(self, configuration):
        """The configured write settings plus what is derived live: a movie
        always runs at the script's fps, not a number typed into YAML, and
        always gets the studio's movie codec, not whatever a preset's
        mov64_codec happens to say.

        The studio was getting unplayable review movies because presets
        (this app's own defaults included) set mov64_codec to H.264, and
        Nuke's H.264 mov writer is not reliably viewable outside Nuke. Every
        movie write now gets DEFAULT_MOVIE_CODEC (Apple ProRes 422 HQ)
        regardless of what a category's YAML says, the same way fps already
        overrides a typed-in number - one setting decides, so a preset
        cannot bring H.264 back by omission. mov64_quality_max and the other
        H264_ONLY_KNOBS are dropped from an inherited preset since they mean
        nothing to ProRes and would otherwise sit on the node unexplained.
        """
        settings = dict(configuration.get("settings") or {})
        if configuration.get("file_type") in MOVIE_FILE_TYPES:
            try:
                fps = autopilot.valid_fps(nuke.root()["fps"].value())
            except Exception:
                fps = None
            if fps:
                settings["mov64_fps"] = fps

            for knob in H264_ONLY_KNOBS:
                settings.pop(knob, None)
            settings["mov64_codec"] = self.__get_movie_codec()
        return settings

    def __set_path(self, node, write_node, render_path, force=False):
        """Puts render_path on the Write's file knob - unless the file was
        edited by hand since the pipeline last set it."""
        tracked = node.knob(KNOB_PATH)
        current = write_node["file"].value()
        if (
            tracked is not None
            and not force
            and current
            and current != tracked.value()
        ):
            return
        self.__set_knob(write_node, "file", render_path)
        if tracked is not None:
            self.__set_knob(node, KNOB_PATH, render_path)

    def __prepare_write(
        self,
        node,
        script_path=None,
        create_dirs=True,
        quiet=False,
        mode="path",
    ):
        """Bring a write node in line with the pipeline

        mode "path" (rendering, saving, version-up): only the render path
        - and only if it is still the one the pipeline wrote. Everything
        else on the node is the artist's.

        mode "full" (creation, picking another category / preset, the
        "apply studio settings" button): the preset's settings too, and
        the path unconditionally.

        Args:
            node (attribute): node to process
            script_path (str, optional): script path to derive the render
                path from instead of the current script
            create_dirs (bool, optional): create the render folder
            quiet (bool, optional): never pop a message up on failure
            mode (str, optional): "path" or "full"

        Returns:
            bool: returns True if processing is completed, False if failed
        """
        full = mode == "full"

        # Get node settings for selected node
        configuration = self.__get_node_settings(node)
        if not configuration:
            if not quiet:
                nuke.message(
                    "Could not find configuration for node %s"
                    % node["name"].value()
                )
            return False

        # Get render path
        render_path, fields = self.__resolve_render_path(
            node, configuration, script_path
        )
        if not render_path:
            if not quiet:
                nuke.message(
                    "Could not work out where to render: save the script "
                    "as a work file first."
                )
            return False

        with _inner_write(node) as write_node:
            if full:
                self.__set_knob(
                    write_node, "file_type", configuration.get("file_type")
                )
                for knob, setting in self.__effective_settings(
                    configuration
                ).items():
                    # channels: auto -> rgba if the input has alpha, else rgb
                    if knob == "channels" and setting == "auto":
                        setting = autopilot.channels_for(
                            self.__input_channels(node)
                        )
                    self.__set_knob(write_node, knob, setting)

                if configuration.get("file_type") in MOVIE_FILE_TYPES:
                    codec_knob = write_node.knob("mov64_codec")
                    if codec_knob is not None and _is_h264(codec_knob.value()):
                        logger.warning(
                            "tk-nuke-writenode: %s ended up on an H.264 codec "
                            "('%s') - '%s' was requested but this Nuke's mov "
                            "writer has no matching menu entry. Check the "
                            "movie_codec app setting against this Nuke "
                            "version's mov64_codec menu." % (
                                write_node["name"].value(),
                                codec_knob.value(),
                                self.__get_movie_codec(),
                            )
                        )

                # Let Nuke itself (local render, F5, the farm) create the
                # folder at render time
                self.__set_knob(write_node, "create_directories", True)

            self.__set_path(node, write_node, render_path, force=full)
            render_file = write_node["file"].value()

        if full:
            self.__set_knob(node, "tile_color", configuration.get("tile_color"))
        self.__refresh_label(node, fields, force=full)

        if create_dirs and render_file:
            # Make sure directory exists
            render_directory = os.path.dirname(render_file)

            # If directory doesn't exist, create it
            if not os.path.isdir(render_directory):
                os.makedirs(render_directory)

        return True

    def __refresh_label(self, node, fields, force=False):
        """Shows what the node renders on the DAG ("main v003") - unless
        the artist put a label of their own on it."""
        try:
            version = int(fields.get("version"))
        except (TypeError, ValueError, AttributeError):
            return
        knob = node.knob("label")
        current = knob.value() if knob is not None else None
        if force or not current or _LABEL_PATTERN.match(current):
            self.__set_knob(
                node, "label", "%s v%03d" % (_sg_value(node, "output"), version)
            )

    def __increment_save(self):
        """Increment save the current script"""

        # Get script template
        script_template = self.app.get_template("template_script_work")
        script_file = nuke.root().name()

        # Get fields
        fields = script_template.get_fields(script_file)

        # Increment version number
        fields["version"] = fields["version"] + 1

        # Calculate path
        new_script_file = script_template.apply_fields(fields).replace(
            os.sep, "/"
        )

        # Save script with incremented path
        nuke.scriptSaveAs(new_script_file)

    def __get_published_path(self, node, path):
        """Calculate path for published render path

        Args:
            node (attribute): node to calculate path for
            path (str): file path set in write node

        Returns:
            str: path used for publishing
        """
        # Get render template and get fields from it
        render_template = self.get_node_render_template(node)
        render_fields = render_template.get_fields(path)

        # Get publish template
        publish_template = self.get_node_publish_template(node)

        # Calculate path with fields from render path
        publish_path = publish_template.apply_fields(render_fields).replace(
            os.sep, "/"
        )

        return publish_path

    @staticmethod
    def __get_frame_sequences(folder, extensions=None, frame_spec=None):
        """Copied from the publisher app, and customized to return
        file sequences with frame lists instead of filenames

        Args:
            folder (str): folder to scan for frame sequences
            extensions (str, optional): extension to search for. Defaults
            to None (all).
            frame_spec (str, optional): if required another frame spec
            can be used
            for returning. Defaults to None (%04d).

        Returns:
            list: containing all frame sequences in specified folder
        """

        FRAME_REGEX = re.compile(r"(.*)([._-])(\d+)\.([^.]+)$", re.IGNORECASE)

        # list of already processed file names
        processed_names = {}

        # examine the files in the folder
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)

            if os.path.isdir(file_path):
                # ignore subfolders
                continue

            # see if there is a frame number
            frame_pattern_match = re.search(FRAME_REGEX, filename)

            if not frame_pattern_match:
                # no frame number detected. carry on.
                continue

            prefix = frame_pattern_match.group(1)
            frame_sep = frame_pattern_match.group(2)
            frame_str = frame_pattern_match.group(3)
            extension = frame_pattern_match.group(4) or ""

            # filename without a frame number.
            file_no_frame = "%s.%s" % (prefix, extension)

            if file_no_frame in processed_names:
                # already processed this sequence. add the framenumber to the list, later we can use this to
                # determine the framerange
                processed_names[file_no_frame]["frame_list"].append(frame_str)
                continue

            if extensions and extension not in extensions:
                # not one of the extensions supplied
                continue

            # make sure we maintain the same padding
            if not frame_spec:
                padding = len(frame_str)
                frame_spec = "%%0%dd" % (padding,)

            seq_filename = "%s%s%s" % (prefix, frame_sep, frame_spec)

            if extension:
                seq_filename = "%s.%s" % (seq_filename, extension)

            # build the path in the same folder
            seq_path = os.path.join(folder, seq_filename)

            # remember each seq path identified and a list of files matching the
            # seq pattern
            processed_names[file_no_frame] = {
                "sequence_path": seq_path,
                "frame_list": [frame_str],
            }

        # build the final list of sequence paths to return
        frame_sequences = []
        for file_no_frame in processed_names:
            seq_info = processed_names[file_no_frame]
            seq_path = seq_info["sequence_path"]

            frame_sequences.append((seq_path, seq_info["frame_list"]))

        return frame_sequences
