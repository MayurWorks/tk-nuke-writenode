# Copyright (c) 2013 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.


from sgtk.platform import Application
import nuke
import os


class TkNukeWriteNode(Application):
    """
    The tk_nuke_writenode entry point. This class is responsible for initializing and tearing down
    the application, handle menu registration etc.
    """

    def init_app(self):
        """
        Called as the application is being initialized
        """

        self.tk_nuke_writenode = self.import_module("tk_nuke_writenode")
        self.handler = self.tk_nuke_writenode.NukeWriteNodeHandler()

        # Registering commands
        # "w" is fully automatic: next free write node, wired to the end
        # of the comp, no dialog. The dialog stays as "custom...".
        create_write_node = lambda: self.handler.create_writenode_auto()
        self.engine.register_command(
            "NFA ShotGrid Write Node",
            create_write_node,
            dict(
                type="node",
                icon="Write.png",
                hotkey="w",
                context=self.context,
            ),
        )

        create_custom_write_node = lambda: self.handler.create_writenode()
        self.engine.register_command(
            "NFA ShotGrid Write Node (custom...)",
            create_custom_write_node,
            dict(
                type="node",
                icon="Write.png",
                context=self.context,
            ),
        )

        read_from_write = lambda: self.handler.read_from_selected()
        self.engine.register_command(
            "Create Read from Write",
            read_from_write,
            dict(
                type="menu",
                icon="Read.png",
                hotkey="ctrl+r",
                context=self.context,
            ),
        )

        # Adding callbacks
        self.handler.add_callbacks()

        # The engine may start with a script already loaded (or be
        # re-initialised by a context change with one open), in which case
        # no load/create callback fires for it.
        self.handler.schedule_autopilot("init")

    def destroy_app(self):
        self.log_debug("Destroying tk-nuke-writenode app")

        self.handler.remove_callbacks()

    def render_local(self, node):
        """Function to start rendering locally. Will set paths and render.

        Args:
            node (object): node to render locally
        """
        self.handler.render_local(node)

    def render_farm(self, node):
        """Function to start rendering on farm. Will set paths and
        use Deadline submission.

        Args:
            node (object): node to submit for render on farm
        """
        self.handler.render_farm(node)

    def knob_changed(self, node, knob):
        """Function called whenever any knob changes on
        the ShotGrid write node

        Args:
            node (attribute): node to process
            knob (attribute): knob that has changed
        """
        self.handler.knob_changed(node, knob)

    def read_from_write(self, node):
        """Creates a read node from the selected write node

        Args:
            node (object): node to create read node from
        """
        self.handler.read_from_write(node)

    def get_all_write_nodes(self):
        """This function will return all existing ShotGrid write nodes
        in the current script

        Returns:
            list: containing every ShotGrid write node
        """
        write_nodes = self.handler.get_all_write_nodes()
        return write_nodes

    def get_node_render_template(self, node):
        """Returns the render template used by the selected node

        Args:
            node (object): specific node to get the render template

        Returns:
            object: render template by ShotGrid template.yml
        """
        render_template = self.handler.get_node_render_template(node)
        return render_template

    def get_node_publish_template(self, node):
        """Returns the publish template used by the selected node

        Args:
            node (object): specific node to get the publish template

        Returns:
            object: publish template by ShotGrid template.yml
        """

        publish_template = self.handler.get_node_publish_template(node)
        return publish_template

    def get_published_status(self, node):
        """Check if selected node is already published

        Args:
            node (object): node to check for publishes

        Returns:
            bool: returns True if node is published, False if not
        """
        is_published = self.handler.get_published_status(node)
        return is_published

    def get_colorspace(self, node):
        """Get the colorspace the selected node is rendering

        Args:
            node (object): node to retrieve current colorspace

        Returns:
            str: colorspace used
        """
        colorspace = self.handler.get_colorspace(node)
        return colorspace

    def on_before_save(self, script_path=None):
        """Provision and sync the write nodes for the script about to be
        saved to script_path (defaults to the current script). Called by
        the workfiles2 scene-operation hook ahead of save / save-as.

        Args:
            script_path (str, optional): path the script is saved to
        """
        self.handler.on_before_save(script_path)

    def sync_write_nodes(self):
        """Re-points every ShotGrid write node at the current script
        version's render path.

        Returns:
            int: number of write nodes synced
        """
        return self.handler.sync_all()

    def update_read_nodes(self):
        """Update all read nodes to use the published path"""
        self.handler.update_read_nodes()

    def convert_placeholder_nodes(self):
        """Converts NoOp nodes used in the template to convert to
        ShotGrid write nodes
        """
        self.handler.convert_placeholder_nodes()

    @staticmethod
    def get_write_nodes():
        """Empty function for legacy reasons to reset all
        render paths on file save, which is not what we want

        Returns:
            list: empty
        """
        return []
