import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from sharpline.db.models import EntityReview, EntityXref, Player, Team
from sharpline.domain.enums import Sport
from sharpline.ingest.resolve import EntityResolver, NeedsReview, Resolved
from sharpline.sports.nba.teams import NBA_TEAMS, seed_nba_teams

pytestmark = pytest.mark.integration


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _player(session: Session, name: str, team_id: int | None = None) -> Player:
    p = Player(sport="nba", full_name=name, team_id=team_id)
    session.add(p)
    session.flush()
    return p


def _xref(session: Session, provider: str, kind: str, provider_id: str) -> EntityXref | None:
    return session.scalar(
        select(EntityXref).where(
            EntityXref.provider == provider,
            EntityXref.entity_kind == kind,
            EntityXref.provider_id == provider_id,
        )
    )


def test_seed_is_idempotent(db_session: Session) -> None:
    assert seed_nba_teams(db_session) == 30
    assert seed_nba_teams(db_session) == 30
    assert _count(db_session, Team) == 30
    assert _count(db_session, EntityXref) == 120


def test_migrations_alone_resolve_every_team_from_each_provider(db_session: Session) -> None:
    """A database brought up by `alembic upgrade head` alone (no seed call) knows all 30 NBA
    teams and their ESPN, Odds API and nba_stats ids (migration 0004)."""
    assert _count(db_session, Team) == 30
    assert _count(db_session, EntityXref) == 120
    resolver = EntityResolver(db_session)
    for t in NBA_TEAMS:
        team_id = db_session.scalar(select(Team.id).where(Team.abbreviation == t.abbr))
        for provider, pid in (
            ("espn", t.espn_abbr),
            ("espn", t.espn_id),
            ("odds_api", t.odds_api_name),
            ("nba_stats", str(t.nba_api_id)),
        ):
            assert resolver.team(provider, pid) == Resolved(team_id)  # type: ignore[arg-type]


def test_unknown_team_goes_to_review(db_session: Session) -> None:
    resolver = EntityResolver(db_session)
    first = resolver.team("odds_api", "Seattle SuperSonics")
    assert isinstance(first, NeedsReview)
    assert resolver.team("odds_api", "Seattle SuperSonics") == first
    review = db_session.get(EntityReview, first.review_id)
    assert review is not None and review.entity_kind == "team" and review.status == "open"


def test_unique_name_match_creates_xref(db_session: Session) -> None:
    player = _player(db_session, "Nicolás Claxton")
    resolver = EntityResolver(db_session)
    result = resolver.player(
        "odds_api", "Nicolas Claxton", "Nicolas Claxton", sport=Sport.NBA, team_id=None
    )
    assert result == Resolved(player.id)
    xref = _xref(db_session, "odds_api", "player", "Nicolas Claxton")
    assert xref is not None and xref.entity_id == player.id
    # now an xref hit
    assert resolver.player(
        "odds_api", "Nicolas Claxton", "Nicolas Claxton", sport=Sport.NBA, team_id=None
    ) == Resolved(player.id)


def test_ambiguous_name_goes_to_review_and_is_idempotent(db_session: Session) -> None:
    a = _player(db_session, "Marcus Morris")
    b = _player(db_session, "Marcus Morris Sr.")
    resolver = EntityResolver(db_session)
    first = resolver.player("espn", "123", "Marcus Morris", sport=Sport.NBA, team_id=None)
    assert isinstance(first, NeedsReview)
    second = resolver.player("espn", "123", "Marcus Morris", sport=Sport.NBA, team_id=None)
    assert second == first
    assert _count(db_session, EntityReview) == 1
    review = db_session.get(EntityReview, first.review_id)
    assert review is not None
    assert {c["id"] for c in review.candidates} == {a.id, b.id}
    assert all({"id", "name", "score"} <= c.keys() for c in review.candidates)


def test_fuzzy_candidates_never_auto_accepted(db_session: Session) -> None:
    _player(db_session, "Alperen Sengun")
    resolver = EntityResolver(db_session)
    result = resolver.player("odds_api", "x", "Alperen Şengün Jr", sport=Sport.NBA, team_id=None)
    assert isinstance(result, Resolved)  # accents + suffix normalize to an exact match
    result = resolver.player("odds_api", "y", "Alperen Sengn", sport=Sport.NBA, team_id=None)
    assert isinstance(result, NeedsReview)
    review = db_session.get(EntityReview, result.review_id)
    assert review is not None and len(review.candidates) == 1
    assert _xref(db_session, "odds_api", "player", "y") is None


def test_unknown_player_from_odds_api_does_not_create_player(db_session: Session) -> None:
    resolver = EntityResolver(db_session)
    result = resolver.player("odds_api", "Nobody", "Nobody Here", sport=Sport.NBA, team_id=None)
    assert isinstance(result, NeedsReview)
    assert _count(db_session, Player) == 0


def test_nba_stats_creates_player_and_xref(db_session: Session) -> None:
    team_id = db_session.scalar(select(Team.id).where(Team.abbreviation == "BOS"))
    resolver = EntityResolver(db_session)
    result = resolver.player(
        "nba_stats", "1628369", "Jayson Tatum", sport=Sport.NBA, team_id=team_id
    )
    assert isinstance(result, Resolved)
    created = db_session.get(Player, result.entity_id)
    assert created is not None and created.full_name == "Jayson Tatum"
    assert created.team_id == team_id and created.sport == "nba"
    assert _xref(db_session, "nba_stats", "player", "1628369") is not None


def test_team_filter_narrows_match(db_session: Session) -> None:
    bos, nyk = db_session.scalars(
        select(Team.id).where(Team.abbreviation.in_(["BOS", "NYK"])).order_by(Team.abbreviation)
    )
    _player(db_session, "Marcus Morris", nyk)
    celtic = _player(db_session, "Marcus Morris", bos)
    resolver = EntityResolver(db_session)
    result = resolver.player("espn", "9", "Marcus Morris", sport=Sport.NBA, team_id=bos)
    assert result == Resolved(celtic.id)


def test_resolve_review_writes_xref(db_session: Session) -> None:
    a = _player(db_session, "Marcus Morris")
    _player(db_session, "Marcus Morris Sr.")
    resolver = EntityResolver(db_session)
    review = resolver.player("espn", "123", "Marcus Morris", sport=Sport.NBA, team_id=None)
    assert isinstance(review, NeedsReview)
    resolver.resolve_review(review.review_id, a.id)
    row = db_session.get(EntityReview, review.review_id)
    assert row is not None and row.status == "resolved" and row.resolved_entity_id == a.id
    xref = _xref(db_session, "espn", "player", "123")
    assert xref is not None and xref.entity_id == a.id
    assert resolver.player(
        "espn", "123", "Marcus Morris", sport=Sport.NBA, team_id=None
    ) == Resolved(a.id)


def test_review_upsert_refreshes_only_that_review(db_session: Session) -> None:
    morris = _player(db_session, "Marcus Morris")
    _player(db_session, "Marcus Morris Sr.")
    resolver = EntityResolver(db_session)
    first = resolver.player("espn", "123", "Marcus Morris", sport=Sport.NBA, team_id=None)
    assert isinstance(first, NeedsReview)
    review = db_session.get(EntityReview, first.review_id)
    assert review is not None and len(review.candidates) == 2  # loaded into the session

    _player(db_session, "Marcus Morris III")  # a third candidate for the same review
    assert morris.full_name == "Marcus Morris"  # (re)load the unrelated object
    again = resolver.player("espn", "123", "Marcus Morris", sport=Sport.NBA, team_id=None)
    assert again == first

    # the pre-loaded review reflects the upsert without an explicit refresh ...
    assert len(review.candidates) == 3
    # ... and unrelated loaded objects were not expired along with it
    assert not inspect(morris).expired_attributes


def test_event_scoped_player_review_resolves_to_a_team_scoped_xref(db_session: Session) -> None:
    bos, nyk = (
        db_session.scalar(select(Team.id).where(Team.abbreviation == a)) for a in ("BOS", "NYK")
    )
    assert bos is not None and nyk is not None
    celtic = _player(db_session, "Marcus Morris", bos)
    _player(db_session, "Marcus Morris", nyk)
    resolver = EntityResolver(db_session)
    first = resolver.player_in_event(
        "odds_api", "Marcus Morris", sport=Sport.NBA, team_ids=(bos, nyk)
    )
    assert isinstance(first, NeedsReview)  # same name on both teams of the event
    resolver.resolve_review(first.review_id, celtic.id)
    assert _xref(db_session, "odds_api", "player", f"Marcus Morris|{bos}") is not None
    again = resolver.player_in_event(
        "odds_api", "Marcus Morris", sport=Sport.NBA, team_ids=(bos, nyk)
    )
    assert again == Resolved(celtic.id)
