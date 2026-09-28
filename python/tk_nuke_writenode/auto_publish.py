# Copyright (c) MayurWorks
#
# Automatic render -> publish trigger.
#
# Prior to this, publishing a rendered write node was 100% manual: an artist
# had to open File > Publish... after every render. This module runs that
# same tk-multi-publish2 flow automatically right after a successful LOCAL
# render, scoped to just the node that rendered.
#
# FARM RENDERS ARE NOT COVERED YET. render_farm() submits to Deadline via
# the gillesvink/NukeDeadlineSubmission package and returns immediately -
# the actual render happens later, in a separate Nuke process on a farm
# blade. Whether that process has the tk-nuke engine (and therefore
# tk-multi-publish2, and this module) initialised at all depends on how
# Deadline is configured to launch Nuke for this pipeline, which has not
# been confirmed. Wiring farm renders into this needs that answered first -
# see the audit notes. Calling auto_publish_write_node() from wherever the
# farm-completion signal actually lives should work as-is once that's
# known, since this function bootstraps its own check for a running engine
# rather than assuming one.

import sgtk

logger = sgtk.platform.get_logger(__name__)


def auto_publish_write_node(node):
    """
    Run the standard tk-multi-publish2 collect -> validate -> publish ->
    finalize flow, scoped to just the single write node that was just
    rendered, with no UI.

    This deliberately reuses the exact collection path an artist gets by
    opening File > Publish... (NukeSessionCollector.collect_sg_writenodes,
    reached via PublishManager.collect_session()) rather than
    PublishManager.collect_files(), because collect_sg_writenodes is the one
    that:
      - builds the full sequence file list via get_frame_sequences and
        attaches publish_template / work_template / colorspace /
        first_frame / last_frame / sg_writenode onto the item - the
        "Validate Render Completeness" publish2 plugin depends on those
        properties being present.
      - already calls tk-nuke-writenode's own get_published_status() before
        even creating an item for a node. That is the pipeline's real
        idempotency guard (a live ShotGrid lookup by filename, not just a
        local flag) - calling collect_files() instead would bypass it
        entirely and risk duplicate PublishedFile records if this ever
        runs twice for the same render.

    collect_session() collects EVERYTHING in the tree (the Nuke script
    itself, plus every not-yet-published write node in the script) so we
    can't just call manager.publish() on the whole tree - that would also
    publish the script and any other unrelated pending write node the
    artist hasn't rendered yet. Instead this builds a task_generator (a
    documented, supported tk-multi-publish2 API - see the validate/publish/
    finalize docstrings in PublishManager) that only yields tasks for the
    item matching this node, and runs validate/publish/finalize scoped to
    just that.

    Args:
        node: the Nuke Write node that was just rendered.

    Returns:
        (bool success, str message). Never raises - failures are logged and
        returned rather than thrown, because this is meant to be called
        from a render-completion callback and must never take down the
        artist's session or block a render that otherwise succeeded.
    """
    render_path = node["file"].value()
    node_name = node.name()

    if not render_path:
        return False, "Node %s has no render path set." % node_name

    engine = sgtk.platform.current_engine()
    if engine is None:
        msg = (
            "No running Toolkit engine - cannot auto-publish %s. Expected "
            "if this Nuke process was not launched through Toolkit."
            % node_name
        )
        logger.warning(msg)
        return False, msg

    publish_app = engine.apps.get("tk-multi-publish2")
    if publish_app is None:
        msg = "tk-multi-publish2 is not configured in this environment."
        logger.warning(msg)
        return False, msg

    try:
        manager = publish_app.create_publish_manager()
        manager.collect_session()
    except Exception as e:
        logger.exception(
            "Auto-publish: collection failed for %s" % node_name
        )
        return False, "Collection failed: %s" % e

    # Match on node identity ONLY. Confirmed against a live collection run
    # (STRM_E1_0070): the same render can appear in the tree twice - once
    # from collect_sg_writenodes() (full properties, incl. sg_writenode) and
    # once from the generic collect_node_outputs() (path/sequence_paths
    # only). Both carry the same "path", so matching on path would publish
    # the same sequence twice.
    target_items = [
        item
        for item in manager.tree
        if item.properties.get("sg_writenode") is node
    ]

    if not target_items:
        # Most likely get_published_status() already found an existing
        # ShotGrid publish with this filename and the collector correctly
        # skipped creating an item at all - not an error, just nothing to
        # do. This is the normal outcome if auto-publish (or a manual
        # publish) already ran for this exact render.
        msg = (
            "Nothing to auto-publish for %s - either it is already "
            "published, or it did not match any collected item."
            % node_name
        )
        logger.info(msg)
        return False, msg

    def _only_our_items(publish_tree):
        for item in target_items:
            for task in item.tasks:
                yield task

    logger.info(
        "Auto-publish: starting for %s (%s)" % (node_name, render_path)
    )

    failed = manager.validate(task_generator=_only_our_items)
    if failed:
        for task, error in failed:
            logger.error(
                "Auto-publish: validation failed for %s, plugin '%s': %s"
                % (node_name, task.plugin.name, error or "no detail")
            )
        return False, "Validation failed (%d task(s)) - see log." % len(
            failed
        )

    try:
        manager.publish(task_generator=_only_our_items)
        manager.finalize(task_generator=_only_our_items)
    except Exception as e:
        logger.exception(
            "Auto-publish: publish/finalize failed for %s" % node_name
        )
        return False, "Publish/finalize failed: %s" % e

    msg = "Published %s (%s)" % (node_name, render_path)
    logger.info("Auto-publish: succeeded - %s" % msg)
    return True, msg
