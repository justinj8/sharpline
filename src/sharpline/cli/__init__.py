"""The `sharpline` command line (spec section 6.2).

    sharpline capture nba --date YYYY-MM-DD [--cap 180] [--dry-run [--offline]]
    sharpline results nba --date YYYY-MM-DD
    sharpline backfill nba-stats --seasons 2023-24 2024-25 2025-26
    sharpline bundle export <capture_run_id> | push <file> | pull <name> | list | load <file>...
    sharpline reparse [--provider odds_api|espn|nba_stats] [--source-id N ...]
    sharpline review list | review resolve <review_id> <entity_id> | review reject <review_id>
    sharpline quota

Commands are grouped by module: capture (providers), bundles, admin (review/reparse/quota).
"""

import typer

from sharpline.cli.admin import quota, reparse, review_app
from sharpline.cli.bundles import bundle_app
from sharpline.cli.capture import backfill_app, capture_app, results_app

app = typer.Typer(
    name="sharpline",
    help="Sharpline data spine: capture, bundles, review and quota.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(capture_app, name="capture", no_args_is_help=True)
app.add_typer(results_app, name="results", no_args_is_help=True)
app.add_typer(backfill_app, name="backfill", no_args_is_help=True)
app.add_typer(bundle_app, name="bundle", no_args_is_help=True)
app.add_typer(review_app, name="review", no_args_is_help=True)
app.command("reparse")(reparse)
app.command("quota")(quota)

__all__ = ["app"]
