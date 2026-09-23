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
            write_names.append(node["output"].value())

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
        """Function called whenever any knob changes on
        the ShotGrid write node

        Args:
            node (attribute): node to process
            knob (attribute): knob that has changed
        """

        if knob.name() == "dataType":
            # __prepare_write() applies file_type/colorspace/etc. from
            # this node's current category+dataType AND recalculates
            # the render path (e.g. switching main -> review changes
            # both the file type and where it renders), so the "file"
            # knob never goes stale relative to what's selected.
            self.__prepare_write(node, quiet=True)

            logger.debug("Updated node settings")

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
            with node:
                file_type = nuke.toNode("Write1")["file_type"].value()
            if file_type in MOVIE_FILE_TYPES:
                return None
            return self.read_from_write(
                node,
                name=AUTO_READ_PREFIX + node["output"].value(),
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
        """Adds callbacks on script load, new script, save and rename"""
        nuke.addOnScriptLoad(self.convert_placeholder_nodes, nodeClass="Root")

        # Autopilot: provision and sync write nodes on every event that
        # can change what they should look like. All of these only
        # *schedule* work (nuke.executeDeferred) so it runs after every
        # other app's Root callback - in particular after
        # tk-nuke-projectsettings has created the plate Read the write
        # nodes are hung off, and after tk-nuke-template has built the
        # comp skeleton.
        nuke.addOnScriptLoad(self._on_script_load, nodeClass="Root")
        nuke.addOnCreate(self._on_root_create, nodeClass="Root")
        nuke.addOnScriptSave(self._on_script_save, nodeClass="Root")
        nuke.addKnobChanged(self._on_root_knob_changed, nodeClass="Root")

    def remove_callbacks(self):
        """Removes callbacks on destroy"""
        nuke.removeOnScriptLoad(
            self.convert_placeholder_nodes, nodeClass="Root"
        )
        nuke.removeOnScriptLoad(self._on_script_load, nodeClass="Root")
        nuke.removeOnCreate(self._on_root_create, nodeClass="Root")
        nuke.removeOnScriptSave(self._on_script_save, nodeClass="Root")
        nuke.removeKnobChanged(self._on_root_knob_changed, nodeClass="Root")

    # ------------------------------------------------------------------
    # Autopilot
    #
    # Write nodes that need no artist: they are created, wired, named,
    # pointed at the right versioned path and colour managed by the
    # pipeline, and they follow the script through every version-up and
    # save-as. Nothing here ever moves, rewires or deletes a node that
    # already exists - it only fills gaps and refreshes paths.
    # ------------------------------------------------------------------

    def __setting_enabled(self, name):
        try:
            return bool(self.app.get_setting(name))
        except Exception:
            return False

    @contextlib.contextmanager
    def __quiet_undo(self):
        """Autopilot edits must not end up on the artist's undo stack."""
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

    @staticmethod
    def _is_sg_write(node):
        return node.Class() == "Group" and node.knob("isShotGridWriteNode") is not None

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
            is_write=self._is_sg_write,
            dependents=lambda node: node.dependent(both, False),
            dependencies=lambda node: node.dependencies(both),
        )

    def _existing_write_info(self):
        """[(category, output)] of the write nodes already in the script."""
        info = []
        for name in self.get_all_write_nodes():
            node = nuke.toNode(name)
            info.append((node["category"].value(), node["output"].value()))
        return info

    @staticmethod
    def _get_provisioned():
        root = nuke.root()
        if autopilot.PROVISIONED_KNOB not in root.knobs():
            return set()
        return autopilot.parse_provisioned(root[autopilot.PROVISIONED_KNOB].value())

    @staticmethod
    def _set_provisioned(categories):
        root = nuke.root()
        if autopilot.PROVISIONED_KNOB not in root.knobs():
            knob = nuke.String_Knob(autopilot.PROVISIONED_KNOB, "provisioned")
            knob.setFlag(nuke.INVISIBLE)
            root.addKnob(knob)
        root[autopilot.PROVISIONED_KNOB].setValue(
            autopilot.format_provisioned(categories)
        )

    def ensure_auto_write_nodes(self, script_path=None, reason=""):
        """
        Creates the write nodes listed in the auto_provision_categories
        setting (main = the EXR, review = the MOV) if the script does not
        have them yet, each one connected to the end of the comp.

        Skipped entirely - silently - when auto_provision is off, when the
        script is not a saved work file (there is no render path to give
        the node yet; the pre-save hook calls this again with the target
        path), or when the script is still empty (tk-nuke-template has yet
        to build the comp). A category is provisioned once per script: the
        hidden root knob remembers it, so a write node an artist deleted
        on purpose is not resurrected on the next open.

        Returns:
            list: names of the nodes created
        """
        if not self.__setting_enabled("auto_provision"):
            return []
        if self._script_fields(script_path) is None:
            return []
        if not nuke.allNodes():
            return []

        wanted = self.app.get_setting("auto_provision_categories") or []
        options = self.__get_write_node_options()
        main_category = self.app.get_setting("main_category_name")
        main_write_name = self.app.get_setting("main_write_name")

        provisioned = self._get_provisioned()
        existing = self._existing_write_info()
        taken = set(output for _, output in existing)
        have_categories = set(category for category, _ in existing)

        created = []
        with nuke.root():
            tail = self._find_tail()
            if tail is not None:
                base_x, base_y = tail.xpos(), tail.ypos() + 90
            else:
                base_x = 0
                base_y = max(n.ypos() for n in nuke.allNodes()) + 150

            for category in wanted:
                if category in provisioned:
                    continue
                if category in have_categories:
                    # made by hand already: nothing to add, nothing to redo
                    provisioned.add(category)
                    continue
                if not options.get(category):
                    logger.debug(
                        "tk-nuke-writenode: auto category '%s' is not "
                        "configured, skipping" % category
                    )
                    continue

                output = autopilot.next_output_name(
                    category, taken, main_category, main_write_name
                )
                if output is None:
                    continue

                node = self.__create_write(
                    options,
                    category,
                    output,
                    options[category][0],
                    input_node=tail,
                    auto=True,
                    xy=(base_x + 150 * len(created), base_y),
                    script_path=script_path,
                )
                created.append(node.name())
                taken.add(output)
                provisioned.add(category)

        if provisioned != self._get_provisioned():
            self._set_provisioned(provisioned)

        if created:
            logger.info(
                "tk-nuke-writenode: auto-created %s (%s)" % (created, reason)
            )
        return created

    def sync_all(self, script_path=None):
        """
        Points every ShotGrid write node in the script at the render path
        of the script's current version, and brings the parts that follow
        the script (file type, colorspace, movie fps, DAG label) up to
        date. Only touches a knob whose value actually differs.

        Returns:
            int: number of write nodes synced
        """
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
                    full=False,
                ):
                    synced += 1
            except Exception:
                logger.warning(
                    "tk-nuke-writenode: could not sync write node %s" % name,
                    exc_info=True,
                )
        return synced

    def run_autopilot(self, reason="", script_path=None):
        """Provision + sync in one go. Never raises."""
        try:
            with self.__quiet_undo():
                self.ensure_auto_write_nodes(script_path, reason)
                self.sync_all(script_path)
        except Exception:
            logger.warning(
                "tk-nuke-writenode: autopilot failed (%s)" % reason,
                exc_info=True,
            )

    def on_before_save(self, script_path=None):
        """Called by the workfiles2 scene-operation hook right before a
        save / save-as is written, with the path about to be saved to, so
        the file on disk already carries the right render paths."""
        self.run_autopilot("before-save", script_path=script_path)

    def schedule_autopilot(self, reason=""):
        """Runs the autopilot once the current callback chain has finished.
        GUI sessions only: a farm / headless render must never edit the
        script it was asked to render."""
        if not getattr(nuke, "GUI", False):
            return
        try:
            nuke.executeDeferred(self.run_autopilot, (reason,))
        except Exception:
            logger.warning(
                "tk-nuke-writenode: could not schedule autopilot",
                exc_info=True,
            )

    def _on_script_load(self):
        self.schedule_autopilot("script-load")

    def _on_root_create(self):
        self.schedule_autopilot("new-script")

    def _on_script_save(self):
        if not getattr(nuke, "GUI", False):
            return
        try:
            self.sync_all()
        except Exception:
            logger.warning(
                "tk-nuke-writenode: sync on save failed", exc_info=True
            )

    def _on_root_knob_changed(self):
        # Save As / version-up changes the script's name, and with it the
        # version every render path is built from.
        try:
            knob = nuke.thisKnob()
            if knob is not None and knob.name() == "name":
                self.schedule_autopilot("script-renamed")
        except Exception:
            pass

    def create_writenode_auto(self):
        """
        The "w" hotkey: creates the next write node immediately - no
        dialog, no naming. Category is the configured default (falling
        back to the first one with a free name), the name is the next
        free one (prerender, prerender2, ...), and it is connected to the
        selected node or, with nothing selected, the end of the comp.
        The old dialog is still available as "custom...".
        """
        options = self.__get_write_node_options()
        if not options:
            nuke.message("No write node categories are configured.")
            return None

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

        upstream = None
        selected = [n for n in nuke.selectedNodes() if not self._is_sg_write(n)]
        if len(selected) == 1 and selected[0].Class() != "Viewer":
            upstream = selected[0]
        else:
            upstream = self._find_tail()

        xy = (upstream.xpos(), upstream.ypos() + 100) if upstream is not None else None

        with nuke.root():
            return self.__create_write(
                options,
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
        """Get all write nodes in list

        Returns:
            list: write nodes in current script
        """

        # Find all groups in script
        all_nodes = nuke.allNodes("Group")

        # Create list to add nodes to
        write_nodes = []

        # Iterate trough all group nodes
        for node in all_nodes:
            # In the write nodes, we have a special knob
            # to help identify this group as a write node
            # If the group has the node "isShotGridWriteNode" we
            # know this is a ShotGrid write node
            if node.knob("isShotGridWriteNode"):

                # If it is a ShotGrid write node, add it to the list
                write_nodes.append(node.name())

        return write_nodes

    @staticmethod
    def go_to_write_node(output_name):
        """Will move the DAG towards the write node using
        the specified output_name

        Args:
            output_name (_type_): _description_
        """
        # Filter all nodes to search for group
        all_nodes = nuke.allNodes("Group")
        for node in all_nodes:

            # If write node has "isShotGridWriteNode" knob, it
            # is indeed a ShotGrid writenode
            if node["isShotGridWriteNode"]:

                # If the node has the specified output_name, we
                # know this is the node we are search for
                if node["output"].value() == output_name:

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
        # Open node to get the write node values
        with node:
            write_node = nuke.toNode("Write1")

            # Get colorspace knob value
            colorspace = write_node["colorspace"].value()

            return colorspace

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
        """Create write node using specified settings

        Args:
            write_node_settings (dict): containing all parameters to setup node
            category (str): category user has chosen to setup node
            output_name (str): output name to render
            data_type (str): datatype to use
            input_node (attribute, optional): node to connect the write to
            auto (bool, optional): created by the autopilot rather than the
                artist: no properties panel, nothing selected/connected by
                accident, no popups and no render folders created yet
            xy (tuple, optional): position (x, y) in the DAG
            script_path (str, optional): script path to derive the render
                path from, if not the current one (i.e. a save-as target)

        Returns:
            attribute: created write node
        """

        if auto:
            # createNode() connects whatever is selected - not wanted here
            for selected in nuke.selectedNodes():
                selected.setSelected(False)

        # Create write node
        created_write = nuke.createNode("sgWrite", inpanel=not auto)

        if input_node is not None:
            created_write.setInput(0, input_node)
        if xy is not None:
            created_write.setXYpos(int(xy[0]), int(xy[1]))

        # Name the node after what it renders: Write_main, Write_review...
        try:
            created_write.setName("Write_%s" % output_name)
        except Exception:
            logger.debug("Could not rename the new write node", exc_info=True)

        # Set output knob value to use specified output_name
        created_write["output"].setValue(output_name)

        # Get all categories and add to knob
        categories = []
        for key, value in write_node_settings.items():
            categories.append(key)

        created_write["category"].setValues(categories)

        # Set category user specified
        created_write["category"].setValue(category)

        # Get all datatypes from pipeline settings
        data_types = write_node_settings.get(category)
        created_write["dataType"].setValues(data_types)

        # Set datatype knob to use datatype user specified
        created_write["dataType"].setValue(data_type)

        # Get the settings the node has to be set to
        configuration = self.__get_node_settings(created_write)
        created_write["tile_color"].setValue(configuration.get("tile_color"))

        # Get internal node settings
        settings = configuration.get("settings")

        # Open to edit internal node
        with created_write:
            # Get node attribute
            write_node = nuke.toNode("Write1")

            # Set file type
            write_node["file_type"].setValue(configuration.get("file_type"))

            # Set all knob settings
            for knob, setting in settings.items():

                # channels: auto -> rgba if the input has alpha, else rgb
                if knob == "channels" and setting == "auto":
                    setting = autopilot.channels_for(
                        self.__input_channels(created_write)
                    )

                try:
                    write_node[knob].setValue(setting)

                except Exception as e:
                    logger.debug(
                        "Could not apply %s to the knob %s, because %s"
                        % (setting, knob, str(e))
                    )

        # Calculate and set the render path immediately, so the node
        # shows where it will render as soon as it's created, instead
        # of leaving the "file" knob blank until the first render.
        # __prepare_write() also (re)applies the settings loop above.
        # Auto-created nodes don't make their render folders yet - that
        # happens when something actually renders.
        self.__prepare_write(
            created_write,
            script_path=script_path,
            create_dirs=not auto,
            quiet=auto,
        )

        return created_write

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
        write_category = node["category"].value()
        data_type = node["dataType"].value()

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

        output = node["output"].value()
        fields["SEQ"] = "FORMAT: %d"
        fields["output"] = output

        # The work template ({Shot}_{Step}_v{version}.nk) has no {name},
        # but every render template asks for one, so resolving would
        # raise TankError for a missing field. The output name is the
        # natural value: it is what tells two write nodes apart.
        fields.setdefault("name", output)

        # Calculate path
        render_path = render_template.apply_fields(fields).replace(os.sep, "/")

        return render_path, fields

    def __calculate_path(self, node, configuration, script_path=None):
        """Render path for node, or None - see __resolve_render_path()"""
        return self.__resolve_render_path(node, configuration, script_path)[0]

    @staticmethod
    def __set_knob(target, name, value):
        """setValue() only when the value differs, and never raises. Keeps a
        no-op sync from dirtying the script."""
        if value is None:
            return
        try:
            knob = target[name]
            if str(knob.value()) != str(value):
                knob.setValue(value)
        except Exception as error:
            logger.debug(
                "Could not apply %s to the knob %s, because %s"
                % (value, name, str(error))
            )

    def __effective_settings(self, configuration):
        """The configured write settings plus what is derived live: a movie
        always runs at the script's fps, not a number typed into YAML."""
        settings = dict(configuration.get("settings") or {})
        if configuration.get("file_type") in MOVIE_FILE_TYPES:
            try:
                fps = autopilot.valid_fps(nuke.root()["fps"].value())
            except Exception:
                fps = None
            if fps:
                settings["mov64_fps"] = fps
        return settings

    def __prepare_write(
        self, node, script_path=None, create_dirs=True, quiet=False, full=True
    ):
        """Set all parameters when rendering.
        Will calculate paths and set them

        Args:
            node (attribute): node to process
            script_path (str, optional): script path to derive the render
                path from instead of the current script
            create_dirs (bool, optional): create the render folder
            quiet (bool, optional): never pop a message up on failure
            full (bool, optional): apply every configured knob. False only
                touches the ones that follow the script (file type, path,
                colorspace, movie fps), so a background sync can't
                overwrite anything an artist tuned on the node.

        Returns:
            bool: returns True if processing is completed, False if failed
        """

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

        settings = self.__effective_settings(configuration)

        # Now we have all the parameters necessary, lets set them
        with node:

            write_node = nuke.toNode("Write1")
            self.__set_knob(write_node, "file_type", configuration.get("file_type"))
            for knob, setting in settings.items():

                # Prevent to change the channels knob
                if knob == "channels":
                    continue
                if not full and knob not in ("colorspace", "mov64_fps"):
                    continue
                self.__set_knob(write_node, knob, setting)

            self.__set_knob(write_node, "file", render_path)

            # Let Nuke itself (local render, F5, the farm) create the
            # folder at render time
            self.__set_knob(write_node, "create_directories", True)

        self.__refresh_label(node, fields)

        if create_dirs:
            # Make sure directory exists
            render_directory = os.path.dirname(render_path)

            # If directory doesn't exist, create it
            if not os.path.isdir(render_directory):
                os.makedirs(render_directory)

        return True

    def __refresh_label(self, node, fields):
        """Shows the version the node renders to on the DAG (label: v003)."""
        try:
            version = int(fields.get("version"))
        except (TypeError, ValueError, AttributeError):
            return
        self.__set_knob(node, "label", "v%03d" % version)

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
