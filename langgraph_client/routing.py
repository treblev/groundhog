"""Deterministic request features and route selection for Groundhog `/ask`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Callable, Iterable


class RouteId(StrEnum):
    STOCK_NOTE_EXACT = "stock_note_exact"
    STOCK_NOTE_SEMANTIC = "stock_note_semantic"
    STOCK_NOTE_AGGREGATE = "stock_note_aggregate"
    STOCK_ALERT_EXACT = "stock_alert_exact"
    WORKOUT_SEMANTIC = "workout_semantic"
    LATEST_PRICE = "latest_price"
    WEEKLY_HEALTH_SUMMARY = "weekly_health_summary"
    WEEKLY_MARKET_SUMMARY = "weekly_market_summary"


@dataclass(frozen=True)
class RequestFeatures:
    domain: str | None = None
    operation: str | None = None
    tickers: tuple[str, ...] = ()
    start_date: str | None = None
    end_date: str | None = None
    timeframe: str | None = None
    direction: str | None = None
    ordering: str = "desc"
    limit: int | None = None
    semantic_intent: bool = False
    undefined_relative_date: bool = False


@dataclass(frozen=True)
class RouteDecision:
    route_id: RouteId
    tool: str
    arguments: dict = field(default_factory=dict)
    answer_instruction: str = ""
    features: RequestFeatures = field(default_factory=RequestFeatures)


@dataclass(frozen=True)
class RouteMatch:
    decision: RouteDecision | None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class RouteSpec:
    route_id: RouteId
    domain: str
    operation: str
    tool: str
    argument_builder: Callable[[RequestFeatures, str], dict]
    answer_instruction: str
    minimum_tickers: int = 0
    maximum_tickers: int | None = None


_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_UNDEFINED_RELATIVE_RE = re.compile(r"\b(?:recently|lately|a while ago)\b", re.IGNORECASE)
_STOCK_HINT_RE = re.compile(
    r"\b(?:stock|stocks|ticker|tickers|share|shares|price|super\s*trend|alert|alerts|note|notes)\b",
    re.IGNORECASE,
)
_ALERT_RE = re.compile(r"\b(?:super\s*trend|supertrend|alert|alerts|triggered|flip|flipped)\b", re.IGNORECASE)
_NOTE_RE = re.compile(r"\b(?:stock\s+notes?|notes?|written|wrote|saved)\b", re.IGNORECASE)
_WORKOUT_RE = re.compile(
    r"\b(?:workout|workouts|amrap|emom|training|exercise|movement|hyrox)\b",
    re.IGNORECASE,
)
_LATEST_PRICE_RE = re.compile(
    r"\b(?:(?:latest|current|closing|close)\s+(?:stock\s+)?price|price\s+(?:of|for))\b",
    re.IGNORECASE,
)
_WEEKLY_HEALTH_SUMMARY_RE = re.compile(
    r"\b(?:weekly|week(?:ly)?\s+(?:ending|summary))\b.*\bhealth\b|\bhealth\b.*\bweekly\b",
    re.IGNORECASE,
)
_WEEKLY_MARKET_SUMMARY_RE = re.compile(
    r"\b(?:weekly|week(?:ly)?\s+(?:ending|summary))\b.*\bmarket\b|\bmarket\b.*\bweekly\b",
    re.IGNORECASE,
)


def looks_like_stock_request(question: str) -> bool:
    """Return whether runtime ticker discovery may be needed for this question."""
    return bool(_STOCK_HINT_RE.search(question))


def _ticker_matches(question: str, symbols: Iterable[str], aliases: dict[str, str]) -> tuple[str, ...]:
    resolved: list[str] = []
    for symbol in sorted({item.strip().upper() for item in symbols if item.strip()}, key=len, reverse=True):
        if re.search(
            rf"(?<![A-Z0-9.\-]){re.escape(symbol)}(?![A-Z0-9.\-])",
            question,
            re.IGNORECASE,
        ) and symbol not in resolved:
            resolved.append(symbol)
    for alias, ticker in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])", question, re.IGNORECASE):
            canonical = ticker.upper()
            if canonical not in resolved:
                resolved.append(canonical)
    return tuple(resolved)


def _limit(question: str) -> int | None:
    for pattern in (
        r"\b(\d+)\s+most\s+recent\b",
        r"\b(?:latest|most\s+recent)\s+(\d+)\b",
    ):
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            return max(1, min(int(match.group(1)), 100))
    if re.search(r"\b(?:the\s+)?most\s+recent\b", question, re.IGNORECASE):
        return 1
    return None


def extract_features(
    question: str,
    symbols: Iterable[str],
    aliases: dict[str, str],
    reference_date: date,
) -> RequestFeatures:
    """Extract only deterministic features; unsupported ambiguity remains explicit."""
    del reference_date  # The summary tools resolve an omitted week to the latest Saturday.
    lowered = question.lower()
    dates = _ISO_DATE_RE.findall(question)
    tickers = _ticker_matches(question, symbols, aliases)
    timeframe = "weekly" if re.search(r"\bweekly\b", lowered) else (
        "daily" if re.search(r"\bdaily\b", lowered) else None
    )
    bullish_word = bool(re.search(r"\b(?:bullish|buy|bought)\b", lowered))
    bearish_word = bool(re.search(r"\b(?:bearish|sell|sold)\b", lowered))
    direction = None
    if bullish_word and not bearish_word:
        direction = "bullish"
    elif bearish_word and not bullish_word:
        direction = "bearish"

    undefined_relative_date = bool(_UNDEFINED_RELATIVE_RE.search(question))
    start_date = dates[0] if dates else None
    end_date = dates[1] if len(dates) > 1 else start_date
    limit = _limit(question)

    if _WEEKLY_HEALTH_SUMMARY_RE.search(question):
        return RequestFeatures(
            domain="weekly_health",
            operation="summary",
            end_date=dates[-1] if dates else None,
        )

    if _WEEKLY_MARKET_SUMMARY_RE.search(question):
        return RequestFeatures(
            domain="weekly_market",
            operation="summary",
            end_date=dates[-1] if dates else None,
        )

    if _LATEST_PRICE_RE.search(question):
        return RequestFeatures(
            domain="stock_price",
            operation="latest",
            tickers=tickers,
            ordering="desc",
            limit=1,
            undefined_relative_date=undefined_relative_date,
        )

    if _ALERT_RE.search(question):
        return RequestFeatures(
            domain="stock_alert",
            operation="latest_per_ticker" if limit else "list",
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            timeframe=timeframe,
            direction=direction,
            ordering="desc",
            limit=limit,
            undefined_relative_date=undefined_relative_date,
        )

    if _NOTE_RE.search(question) or (
        tickers and re.search(r"\b(?:shares?|bought|purchased)\b", question, re.IGNORECASE)
    ):
        aggregate = bool(
            tickers
            and re.search(r"\bhow\s+many\s+shares?\b", question, re.IGNORECASE)
            and re.search(r"\b(?:buy|bought|purchase|purchased)\b", question, re.IGNORECASE)
        )
        semantic = bool(
            re.search(r"\b(?:which|find|show)\b.*\bnotes?\b.*\b(?:says?|mentions?|discusses?)\b", question, re.IGNORECASE)
        )
        return RequestFeatures(
            domain="stock_note",
            operation="aggregate" if aggregate else ("semantic" if semantic else "list"),
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            ordering="desc",
            limit=100,
            semantic_intent=semantic,
            undefined_relative_date=undefined_relative_date,
        )

    if _WORKOUT_RE.search(question):
        exact_or_aggregate = bool(
            dates
            or re.search(r"\b(?:count|how\s+many|average|total|on\s+20\d{2}-)\b", question, re.IGNORECASE)
        )
        return RequestFeatures(
            domain="workout",
            operation="structured" if exact_or_aggregate else "semantic",
            start_date=start_date,
            end_date=end_date,
            semantic_intent=not exact_or_aggregate,
            undefined_relative_date=undefined_relative_date,
        )

    return RequestFeatures(undefined_relative_date=undefined_relative_date)


def _latest_price_arguments(features: RequestFeatures, _question: str) -> dict:
    return {"ticker": features.tickers[0]}


def _note_arguments(features: RequestFeatures, _question: str) -> dict:
    arguments: dict = {
        "tickers": list(features.tickers),
        "active_only": True,
        "order": features.ordering,
        "limit": features.limit or 100,
    }
    if features.start_date:
        arguments["start_date"] = features.start_date
    if features.end_date:
        arguments["end_date"] = features.end_date
    return arguments


def _semantic_note_arguments(features: RequestFeatures, question: str) -> dict:
    return {
        "query": question,
        "domain": "stock_note",
        "top_k": 10,
        "ticker": features.tickers[0],
    }


def _alert_arguments(features: RequestFeatures, _question: str) -> dict:
    arguments: dict = {
        "tickers": list(features.tickers),
        "order": features.ordering,
        "limit": features.limit or 100,
        "latest_per_ticker": features.operation == "latest_per_ticker",
    }
    for key in ("start_date", "end_date", "timeframe", "direction"):
        value = getattr(features, key)
        if value:
            arguments[key] = value
    return arguments


def _workout_arguments(_features: RequestFeatures, question: str) -> dict:
    return {"query": question, "domain": "workout", "top_k": 5}


def _weekly_summary_arguments(features: RequestFeatures, _question: str) -> dict:
    return {"week_end": features.end_date} if features.end_date else {}


ROUTE_REGISTRY: tuple[RouteSpec, ...] = (
    RouteSpec(
        RouteId.WEEKLY_HEALTH_SUMMARY,
        "weekly_health",
        "summary",
        "get_weekly_health_summary",
        _weekly_summary_arguments,
        "Describe the Sunday-through-Saturday health, activity, and sleep results. Distinguish daily active minutes from recorded activity minutes.",
    ),
    RouteSpec(
        RouteId.WEEKLY_MARKET_SUMMARY,
        "weekly_market",
        "summary",
        "get_weekly_market_summary",
        _weekly_summary_arguments,
        "Describe Bitcoin's weekly price trend, its latest Supertrend state as of week end, and the important alert balance and flips.",
    ),
    RouteSpec(
        RouteId.LATEST_PRICE,
        "stock_price",
        "latest",
        "get_latest_price",
        _latest_price_arguments,
        "State the latest closing price and its date.",
        minimum_tickers=1,
        maximum_tickers=1,
    ),
    RouteSpec(
        RouteId.STOCK_NOTE_EXACT,
        "stock_note",
        "list",
        "query_stock_notes",
        _note_arguments,
        "List the returned active notes and their dates; an empty rows list means no matching notes.",
        minimum_tickers=1,
    ),
    RouteSpec(
        RouteId.STOCK_NOTE_SEMANTIC,
        "stock_note",
        "semantic",
        "search_documents",
        _semantic_note_arguments,
        "Return only notes that match the requested meaning and their context.",
        minimum_tickers=1,
        maximum_tickers=1,
    ),
    RouteSpec(
        RouteId.STOCK_NOTE_AGGREGATE,
        "stock_note",
        "aggregate",
        "query_stock_notes",
        _note_arguments,
        "Add only explicit purchased-share quantities in the evidence, show the arithmetic, and flag ambiguity.",
        minimum_tickers=1,
        maximum_tickers=1,
    ),
    RouteSpec(
        RouteId.STOCK_ALERT_EXACT,
        "stock_alert",
        "list",
        "query_stock_alerts",
        _alert_arguments,
        "Answer from the exact alert rows, including ticker, date, and bullish/bearish direction derived from alert_type. An empty rows list means no matching alert.",
    ),
    RouteSpec(
        RouteId.STOCK_ALERT_EXACT,
        "stock_alert",
        "latest_per_ticker",
        "query_stock_alerts",
        _alert_arguments,
        "Answer from the exact latest-per-ticker alert rows, including ticker, date, and bullish/bearish direction derived from alert_type. An empty rows list means no matching alert.",
    ),
    RouteSpec(
        RouteId.WORKOUT_SEMANTIC,
        "workout",
        "semantic",
        "search_documents",
        _workout_arguments,
        "Return the best matching stored workout and explain the match from evidence only.",
    ),
)


def select_route(features: RequestFeatures, question: str) -> RouteMatch:
    """Select a route only when its domain, operation, and required fields are settled."""
    if features.undefined_relative_date:
        return RouteMatch(None, "undefined_relative_date")

    spec = next(
        (
            candidate
            for candidate in ROUTE_REGISTRY
            if candidate.domain == features.domain and candidate.operation == features.operation
        ),
        None,
    )
    if spec is None:
        reason = "no_confident_route" if features.domain is None else f"unsupported_{features.domain}_{features.operation}"
        return RouteMatch(None, reason)

    ticker_count = len(features.tickers)
    if ticker_count < spec.minimum_tickers:
        reason = (
            "stock_note_requires_resolved_ticker"
            if spec.domain == "stock_note"
            else f"{spec.route_id}_requires_resolved_ticker"
        )
        return RouteMatch(None, reason)
    if spec.maximum_tickers is not None and ticker_count > spec.maximum_tickers:
        return RouteMatch(None, f"{spec.route_id}_has_ambiguous_tickers")

    return RouteMatch(RouteDecision(
        route_id=spec.route_id,
        tool=spec.tool,
        arguments=spec.argument_builder(features, question),
        answer_instruction=spec.answer_instruction,
        features=features,
    ))
