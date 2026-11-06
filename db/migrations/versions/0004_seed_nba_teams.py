"""seed the 30 NBA teams and their provider xrefs

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06 10:00:00.000000

Data migration. Team resolution must be exact from the first capture on (spec section 5.4),
so every migrated database gets the NBA teams and their ESPN (abbreviation and id), Odds API
(full name) and nba_stats ids. The rows come from `sharpline.sports.nba.teams`, the same
source of truth as `seed_nba_teams`; nothing is copied here. Inserts only, ON CONFLICT DO
NOTHING, so it is a no-op on a database that was already seeded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sharpline.domain.enums import Sport
from sharpline.sports.nba.teams import NBA_TEAMS, team_xrefs

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INSERT_TEAM = sa.text(
    "INSERT INTO teams (sport, name, abbreviation) VALUES (:sport, :name, :abbr) "
    "ON CONFLICT (sport, abbreviation) DO NOTHING"
)
INSERT_XREF = sa.text(
    "INSERT INTO entity_xref (entity_kind, entity_id, provider, provider_id) "
    "SELECT 'team', t.id, :provider, :provider_id FROM teams t "
    "WHERE t.sport = :sport AND t.abbreviation = :abbr "
    "ON CONFLICT (provider, entity_kind, provider_id) DO NOTHING"
)


def upgrade() -> None:
    bind = op.get_bind()
    sport = Sport.NBA.value
    bind.execute(INSERT_TEAM, [{"sport": sport, "name": t.name, "abbr": t.abbr} for t in NBA_TEAMS])
    bind.execute(
        INSERT_XREF,
        [
            {"sport": sport, "abbr": t.abbr, "provider": provider, "provider_id": provider_id}
            for t in NBA_TEAMS
            for provider, provider_id in team_xrefs(t)
        ],
    )


def downgrade() -> None:
    # Intentionally a no-op: by now events, players and xrefs reference these teams, and
    # deleting them would either fail on the foreign keys or take real data with them.
    # Downgrading past 0001 drops the tables (and these rows) anyway.
    pass
