"""Pure parsers: Odds API payloads into normalized rows. No I/O."""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sharpline.domain.enums import Side, Sport
from sharpline.domain.odds import InvalidOddsError, validate_american
from sharpline.domain.time import ensure_utc
from sharpline.sources.odds_api.client import PROVIDER
from sharpline.sources.odds_api.markets import UnsupportedMarket, describe_market
from sharpline.sources.rows import EventRow, OddsRow

log = logging.getLogger(__name__)

_NAME_SIDES = {
    "over": Side.OVER,
    "under": Side.UNDER,
    "yes": Side.YES,
    "no": Side.NO,
}


def _parse_time(raw: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))


def parse_events(payload: list[dict[str, Any]]) -> list[EventRow]:
    return [
        EventRow(
            provider=PROVIDER,
            provider_event_id=e["id"],
            sport=Sport.NBA,
            home_team=e["home_team"],
            away_team=e["away_team"],
            commence_time=_parse_time(e["commence_time"]),
        )
        for e in payload
    ]


def parse_odds(
    payload: list[dict[str, Any]] | dict[str, Any], captured_at: datetime
) -> list[OddsRow]:
    """Handle both the list (game odds) and single-event (props) response shapes."""
    captured_at = ensure_utc(captured_at)
    events = payload if isinstance(payload, list) else [payload]
    rows: list[OddsRow] = []
    for event in events:
        rows.extend(_parse_event(event, captured_at))
    return rows


def _side(name: str, market_type_is_team: bool, home: str, away: str) -> Side | None:
    if market_type_is_team and name == home:
        return Side.HOME
    if market_type_is_team and name == away:
        return Side.AWAY
    return _NAME_SIDES.get(name.lower())


def _parse_event(event: dict[str, Any], captured_at: datetime) -> list[OddsRow]:
    event_id: str = event["id"]
    home, away = event["home_team"], event["away_team"]
    rows: list[OddsRow] = []
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            key: str = market["key"]
            try:
                spec = describe_market(key)
            except UnsupportedMarket:
                log.warning("skipping unsupported market %s (event %s)", key, event_id)
                continue
            team_market = spec.market_type.value in ("moneyline", "spread")
            for outcome in market.get("outcomes", []):
                name: str = outcome["name"]
                side = _side(name, team_market, home, away)
                if side is None or (team_market and side not in (Side.HOME, Side.AWAY)):
                    log.warning(
                        "dropping outcome %r in %s/%s (event %s): not mappable to a side",
                        name,
                        book["key"],
                        key,
                        event_id,
                    )
                    continue
                try:
                    price = validate_american(int(outcome["price"]))
                except InvalidOddsError:
                    log.warning(
                        "dropping invalid price %s in %s/%s (event %s)",
                        outcome["price"],
                        book["key"],
                        key,
                        event_id,
                    )
                    continue
                point = outcome.get("point")
                rows.append(
                    OddsRow(
                        provider_event_id=event_id,
                        market_key=key,
                        market_type=spec.market_type,
                        period=spec.period,
                        stat=spec.stat,
                        player_name=outcome.get("description") if spec.stat else None,
                        book=book["key"],
                        side=side,
                        price=price,
                        line=None if point is None else Decimal(str(point)),
                        captured_at=captured_at,
                    )
                )
    return rows
