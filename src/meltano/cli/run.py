"""meltano run command and supporting functions."""
from __future__ import annotations

import click
import structlog

from meltano.core.block.blockset import BlockSet
from meltano.core.block.parser import BlockParser, validate_block_sets
from meltano.core.block.plugin_command import PluginCommandBlock
from meltano.core.legacy_tracking import LegacyTracker
from meltano.core.logging.utils import change_console_log_level
from meltano.core.project import Project
from meltano.core.project_settings_service import ProjectSettingsService
from meltano.core.runner import RunnerError
from meltano.core.tracking import BlockEvents, CliEvent, Tracker
from meltano.core.tracking.contexts.plugins import PluginsTrackingContext
from meltano.core.utils import click_run_async

from . import CliError, cli
from .params import pass_project
from .utils import InstrumentedCmd

logger = structlog.getLogger(__name__)


@cli.command(
    cls=InstrumentedCmd, short_help="[preview] Run a set of plugins in series."
)
@click.option(
    "--dry-run",
    help="Do not run, just parse the invocation, validate it, and explain what would be executed.",
    is_flag=True,
)
@click.option(
    "--full-refresh",
    help="Perform a full refresh (ignore state left behind by any previous runs). Applies to all pipelines.",
    is_flag=True,
)
@click.option(
    "--no-state-update",
    help="Run without state saving. Applies to all pipelines.",
    is_flag=True,
)
@click.option(
    "--force",
    "-f",
    help="Force a new run even if a pipeline with the same State ID is already present. Applies to all pipelines.",
    is_flag=True,
)
@click.argument(
    "blocks",
    nargs=-1,
)
@pass_project(migrate=True)
@click.pass_context
@click_run_async
async def run(
    ctx: click.Context,
    project: Project,
    dry_run: bool,
    full_refresh: bool,
    no_state_update: bool,
    force: bool,
    blocks: list[str],
):
    """
    Run a set of command blocks in series.

    Blocks are specified as a list of plugin names, e.g.
    `meltano run some_extractor some_loader some_plugin:some_command` and are run in the order they are specified
    from left to right. A failure in any block will cause the entire run to abort.

    Multiple commmand blocks can be chained together or repeated, and tap/target pairs will automatically be linked:

        `meltano run tap-gitlab target-postgres dbt:test dbt:run`\n
        `meltano run tap-gitlab target-postgres tap-salesforce target-mysql ...`\n
        `meltano run tap-gitlab target-postgres dbt:run tap-postgres target-bigquery ...`\n

    When running within an active environment, meltano run activates incremental job support. State ID's are autogenerated
    using the format `{active_environment.name}:{extractor_name}-to-{loader_name}` for each extract/load pair found:

        `meltano --environment=prod run tap-gitlab target-postgres tap-salesforce target-mysql`\n

    The above command will create two jobs with state IDs `prod:tap-gitlab-to-target-postgres` and `prod:tap-salesforce-to-target-mysql`.

    This a preview feature - its functionality and cli signature is still evolving.

    \b\nRead more at https://docs.meltano.com/reference/command-line-interface#run
    """
    if dry_run:
        if not ProjectSettingsService.config_override.get("cli.log_level"):
            logger.info("Setting 'console' handler log level to 'debug' for dry run")
            change_console_log_level()

    tracker: Tracker = ctx.obj["tracker"]
    legacy_tracker: LegacyTracker = ctx.obj["legacy_tracker"]

    parser_blocks = []  # noqa: F841
    try:
        parser = BlockParser(
            logger, project, blocks, full_refresh, no_state_update, force
        )
        parsed_blocks = list(parser.find_blocks(0))
        if not parsed_blocks:
            tracker.track_command_event(CliEvent.aborted)
            logger.info("No valid blocks found.")
            return
    except Exception as parser_err:
        tracker.track_command_event(CliEvent.aborted)
        raise parser_err

    if validate_block_sets(logger, parsed_blocks):
        logger.debug("All ExtractLoadBlocks validated, starting execution.")
    else:
        tracker.track_command_event(CliEvent.aborted)
        raise CliError("Some ExtractLoadBlocks set failed validation.")
    try:
        await _run_blocks(tracker, parsed_blocks, dry_run=dry_run)
    except Exception as err:
        tracker.track_command_event(CliEvent.failed)
        raise err
    tracker.track_command_event(CliEvent.completed)
    legacy_tracker.track_meltano_run(blocks)


async def _run_blocks(
    tracker: Tracker,
    parsed_blocks: list[BlockSet | PluginCommandBlock],
    dry_run: bool,
) -> None:
    for idx, blk in enumerate(parsed_blocks):
        blk_name = blk.__class__.__name__
        tracking_ctx = PluginsTrackingContext.from_block(blk)
        with tracker.with_contexts(tracking_ctx):
            tracker.track_block_event(blk_name, BlockEvents.initialized)
        if dry_run:
            if isinstance(blk, BlockSet):
                logger.info(
                    f"Dry run, but would have run block {idx + 1}/{len(parsed_blocks)}.",
                    block_type=blk_name,
                    comprised_of=[plugin.string_id for plugin in blk.blocks],
                )
            elif isinstance(blk, PluginCommandBlock):
                logger.info(
                    f"Dry run, but would have run block {idx + 1}/{len(parsed_blocks)}.",
                    block_type=blk_name,
                    comprised_of=f"{blk.string_id}:{blk.command}",
                )
            continue

        try:
            await blk.run()
        except RunnerError as err:
            logger.error(
                "Block run completed.",
                set_number=idx,
                block_type=blk_name,
                success=False,
                err=err,
                exit_codes=err.exitcodes,
            )
            with tracker.with_contexts(tracking_ctx):
                tracker.track_block_event(blk_name, BlockEvents.failed)
            raise CliError(
                f"Run invocation could not be completed as block failed: {err}"
            ) from err
        except Exception as bare_err:  # make sure we also fire block failed events for all other exceptions
            with tracker.with_contexts(tracking_ctx):
                tracker.track_block_event(blk_name, BlockEvents.failed)
            raise bare_err

        logger.info(
            "Block run completed.",
            set_number=idx,
            block_type=blk.__class__.__name__,
            success=True,
            err=None,
        )
        with tracker.with_contexts(tracking_ctx):
            tracker.track_block_event(blk_name, BlockEvents.completed)
