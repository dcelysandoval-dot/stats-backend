import os
import math
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
# FÚTBOL ANALYTICS - BACKEND V2.2
# Sportmonks principal + API-Football fallback + autenticación V3 robusta
# ============================================================

APP_VERSION = "2.2.0"
# Sportmonks uses numeric season IDs (e.g. 28083), while API-Football
# uses calendar years (e.g. 2026). Keep them separate.
CURRENT_SEASON_YEAR = int(os.getenv("CURRENT_SEASON_YEAR", os.getenv("CURRENT_SEASON", "2026")))
SPORTMONKS_DEFAULT_SEASON_ID = int(
    os.getenv("SPORTMONKS_DEFAULT_SEASON_ID", "28083")
)
# Backwards-compatible alias for any existing code that still imports it.
CURRENT_SEASON = CURRENT_SEASON_YEAR

app = FastAPI(
    title="Fútbol Analytics API",
    version=APP_VERSION,
    description=(
        "Player Market Scanner con Sportmonks como fuente principal, "
        "API-Football como respaldo, alineaciones, estadísticas, "
        "proyecciones, Value Edge y bankroll."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# CONFIGURACIÓN DE APIS
# ============================================================

SPORTMONKS_TOKEN = os.getenv("SPORTMONKS_API_TOKEN", "").strip()
SPORTMONKS_URL = "https://api.sportmonks.com/v3"

APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "").strip()
APIFOOTBALL_URL = "https://v3.football.api-sports.io"

SUPPORTED_MARKETS = [
    "Remates",
    "Remates a puerta",
    "Goles",
    "Asistencias",
    "Faltas",
    "Tarjetas",
]

MIN_STAKE_PERCENT = 1.0
DEFAULT_STAKE_PERCENT = 2.0
MAX_STAKE_PERCENT = 3.0

bets = []
ALLOWED_BET_RESULTS = {"PENDIENTE", "GANADA", "PERDIDA", "ANULADA"}
TEAM_PLAYER_CACHE = {}
TEAM_PLAYER_CACHE_TTL = 300
PLAYER_STATS_CACHE = {}
PLAYER_STATS_CACHE_TTL = 300
RECENT_FORM_CACHE = {}
RECENT_FORM_CACHE_TTL = 300
RECENT_FORM_MATCHES = 8
RECENT_FORM_LOOKBACK_DAYS = 120


# ============================================================
# HELPERS
# ============================================================

def safe_number(value) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if "total" in value:
            return safe_number(value["total"])
        if "value" in value:
            return safe_number(value["value"])
    try:
        return float(value)
    except Exception:
        return 0.0


def round2(value) -> float:
    return round(safe_number(value), 2)


def api_football_season_year(season: Optional[int] = None) -> int:
    """Return a calendar year suitable for API-Football."""
    if season is None:
        return CURRENT_SEASON_YEAR

    # Sportmonks season IDs are large integers (e.g. 28083);
    # API-Football expects a year such as 2026.
    if season > 10000:
        return CURRENT_SEASON_YEAR

    return season


def implied_probability(odds: float) -> float:
    if odds <= 1:
        return 0.0
    return round((1.0 / odds) * 100.0, 2)


def value_edge(probability_fa: float, odds: float) -> float:
    return round(probability_fa - implied_probability(odds), 2)


def fa_rating(probability_fa: float, edge: float, confidence: float = 70) -> int:
    score = (
        probability_fa * 0.45
        + max(edge, 0) * 1.5
        + confidence * 0.25
    )
    return round(max(0, min(score, 100)))


def risk_from_rating(rating: int) -> str:
    if rating >= 80:
        return "BAJO"
    if rating >= 70:
        return "MEDIO"
    if rating >= 60:
        return "MODERADO"
    return "ALTO"


def signal_from_edge(edge: float) -> str:
    if edge >= 15:
        return "OPORTUNIDAD ALTA"
    if edge >= 8:
        return "OPORTUNIDAD MEDIA"
    if edge > 0:
        return "VALOR BAJO"
    return "SIN VALOR"


def classify_data_quality(minutes: float, appearances: float):
    """Classify historical sample size using both minutes and appearances."""
    minutes = safe_number(minutes)
    appearances = safe_number(appearances)

    if minutes >= 900 and appearances >= 10:
        return "ALTA", f"Muestra sólida: {int(appearances)} apariciones / {int(minutes)} min"
    if minutes >= 450 and appearances >= 5:
        return "MEDIA", f"Muestra intermedia: {int(appearances)} apariciones / {int(minutes)} min"
    if minutes > 0 or appearances > 0:
        return "BAJA", f"Muestra inicial: {int(appearances)} apariciones / {int(minutes)} min"
    return "BAJA", "Sin minutos registrados en la temporada"


def calculate_data_confidence(
    minutes: float,
    appearances: float,
    starts: float,
    starter: bool,
    recent_matches: int = 0,
) -> int:
    """Return a sample-aware confidence score from 25 to 95.

    The score is deliberately separate from probability/rating. A confirmed
    starter can still have low confidence when the historical sample is tiny.
    """
    minutes = safe_number(minutes)
    appearances = safe_number(appearances)
    starts = safe_number(starts)
    recent_matches = max(0, int(safe_number(recent_matches)))

    score = 40
    if starter:
        score += 10

    if appearances >= 10:
        score += 22
    elif appearances >= 6:
        score += 15
    elif appearances >= 3:
        score += 9
    elif appearances >= 1:
        score += 4

    if minutes >= 900:
        score += 18
    elif minutes >= 450:
        score += 12
    elif minutes >= 180:
        score += 6
    elif minutes > 0:
        score += 3

    if appearances > 0:
        start_rate = starts / appearances
        if start_rate >= 0.75:
            score += 4
        elif start_rate >= 0.50:
            score += 2

    # Recent form is additional evidence, but it cannot compensate fully
    # for a tiny season sample. Keep the bonus deliberately capped.
    if recent_matches >= 5:
        score += 8
    elif recent_matches >= 3:
        score += 5
    elif recent_matches >= 1:
        score += 2

    if minutes == 0 and appearances == 0 and recent_matches == 0:
        score -= 10

    return int(max(25, min(95, round(score))))


def confidence_band(confidence: float) -> str:
    confidence = safe_number(confidence)
    if confidence >= 85:
        return "ALTA"
    if confidence >= 70:
        return "MEDIA"
    if confidence >= 60:
        return "PRECAUCIÓN"
    return "NO PUBLICAR"


def is_publishable_confidence(confidence: float) -> bool:
    # PRECAUCIÓN (60-69) is visible for review but is not a publishable pick.
    return safe_number(confidence) >= 70


def adjust_rating_for_data_quality(rating: float, confidence: float) -> int:
    """Penalize the final rating when the historical sample is weak."""
    rating = safe_number(rating)
    confidence = safe_number(confidence)

    if confidence >= 85:
        multiplier = 1.00
    elif confidence >= 70:
        multiplier = 0.92
    elif confidence >= 60:
        multiplier = 0.80
    else:
        multiplier = 0.60

    return int(round(max(0, min(100, rating * multiplier))))


def calculate_stake(bankroll: float, stake_percent: float) -> float:
    pct = max(MIN_STAKE_PERCENT, min(stake_percent, MAX_STAKE_PERCENT))
    return round(bankroll * pct / 100.0, 2)


def poisson_probability_at_least(k: int, lam: float) -> float:
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0

    term = math.exp(-lam)
    cumulative = term

    for x in range(1, k):
        term *= lam / x
        cumulative += term

    return max(0.0, min(1.0, 1.0 - cumulative))


def probability_over(line: float, projection: float) -> float:
    target = math.floor(line) + 1
    return round(poisson_probability_at_least(target, projection) * 100, 2)


def probability_under(line: float, projection: float) -> float:
    target = math.ceil(line) - 1
    if target < 0:
        return 0.0
    at_least = poisson_probability_at_least(target + 1, projection)
    return round((1.0 - at_least) * 100, 2)


# ============================================================
# SPORTMONKS CLIENT
# ============================================================

async def sportmonks_get(
    endpoint: str,
    params: Optional[dict] = None,
) -> dict:
    """
    Cliente Sportmonks V3.

    Sportmonks admite el token como:
      1) Header Authorization: <TOKEN>
      2) Query parameter api_token=<TOKEN>

    Usamos primero el header sin prefijo "Bearer", que es el formato
    indicado actualmente en la documentación de API 3.0. Si Sportmonks
    responde 401, hacemos un segundo intento usando api_token como
    parámetro de consulta para cubrir ambas variantes de autenticación.
    """
    if not SPORTMONKS_TOKEN:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": "SPORTMONKS_API_TOKEN no está configurada en Render.",
        }

    url = SPORTMONKS_URL + endpoint
    base_params = dict(params or {})
    headers = {
        "Authorization": SPORTMONKS_TOKEN,
        "Accept": "application/json",
    }

    async def make_request(request_headers: dict, request_params: dict):
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.get(
                url,
                headers=request_headers,
                params=request_params,
            )

    try:
        # Intento principal: Authorization: <TOKEN>
        response = await make_request(headers, base_params)

        # Compatibilidad: si devuelve 401, reintentamos con ?api_token=<TOKEN>
        if response.status_code == 401:
            query_params = dict(base_params)
            query_params["api_token"] = SPORTMONKS_TOKEN
            response = await make_request(
                {"Accept": "application/json"},
                query_params,
            )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code != 200:
            upstream_message = None
            if isinstance(data, dict):
                upstream_message = (
                    data.get("message")
                    or (data.get("error") if isinstance(data.get("error"), str) else None)
                )

            return {
                "ok": False,
                "status_code": response.status_code,
                "data": data,
                "error": (
                    f"Sportmonks respondió HTTP {response.status_code}"
                    + (f": {upstream_message}" if upstream_message else ".")
                ),
            }

        if isinstance(data, dict) and data.get("message") and not data.get("data"):
            return {
                "ok": False,
                "status_code": response.status_code,
                "data": data,
                "error": str(data.get("message")),
            }

        return {
            "ok": True,
            "status_code": response.status_code,
            "data": data,
            "error": None,
        }

    except httpx.RequestError as exc:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": f"Error de conexión con Sportmonks: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": str(exc),
        }


# ============================================================
# API-FOOTBALL CLIENT (FALLBACK)
# ============================================================

async def apifootball_get(
    endpoint: str,
    params: Optional[dict] = None,
) -> dict:
    if not APIFOOTBALL_KEY:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": "APIFOOTBALL_KEY no está configurada en Render.",
        }

    headers = {"x-apisports-key": APIFOOTBALL_KEY}

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(
                APIFOOTBALL_URL + endpoint,
                headers=headers,
                params=params or {},
            )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code != 200:
            return {
                "ok": False,
                "status_code": response.status_code,
                "data": data,
                "error": f"API-Football respondió HTTP {response.status_code}.",
            }

        errors = data.get("errors", []) if isinstance(data, dict) else []
        if errors:
            return {
                "ok": False,
                "status_code": response.status_code,
                "data": data,
                "error": str(errors),
            }

        return {
            "ok": True,
            "status_code": response.status_code,
            "data": data,
            "error": None,
        }

    except httpx.RequestError as exc:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": f"Error de conexión con API-Football: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": str(exc),
        }


# ============================================================
# SPORTMONKS NORMALIZACIÓN
# ============================================================

SPORTMONKS_STAT_TYPES = {
    42: "shots",
    52: "goals",
    56: "fouls_committed",
    79: "assists",
    84: "yellow_cards",
    83: "red_cards",
    86: "shots_on_target",
    96: "fouls_drawn",
    117: "key_passes",
    118: "rating",
    119: "minutes",
    321: "appearances",
    322: "starts",
}


def stat_value(detail) -> float:
    value = detail.get("value")

    if isinstance(value, dict):
        for key in ("total", "value", "count"):
            if key in value and value[key] is not None:
                return safe_number(value[key])

    return safe_number(value)


def blend_season_and_recent_per90(
    season_stats: dict,
    recent_matches: list,
    key: str,
    expected_minutes: float,
) -> dict:
    """Blend season and recent per-90 rates with a sample-aware recency weight.

    Recent form is deliberately conservative: one or two matches cannot
    override a meaningful season sample. The recent window receives 25%, 45%,
    or 60% weight once it reaches 1, 3, or 5 appearances respectively.
    """
    season_minutes = safe_number((season_stats or {}).get("minutes"))
    season_total = safe_number((season_stats or {}).get(key))
    season_per90 = (
        season_total / season_minutes * 90.0
        if season_minutes > 0 else 0.0
    )

    valid = [
        row for row in (recent_matches or [])
        if safe_number(row.get("minutes")) > 0
    ]
    recent_minutes = sum(safe_number(row.get("minutes")) for row in valid)
    recent_total = sum(safe_number(row.get(key)) for row in valid)
    recent_appearances = len(valid)
    recent_per90 = (
        recent_total / recent_minutes * 90.0
        if recent_minutes > 0 else 0.0
    )

    if recent_appearances >= 5:
        recent_weight = 0.60
    elif recent_appearances >= 3:
        recent_weight = 0.45
    elif recent_appearances >= 1:
        recent_weight = 0.25
    else:
        recent_weight = 0.0

    if season_minutes <= 0:
        blended_per90 = recent_per90
    elif recent_appearances == 0:
        blended_per90 = season_per90
    else:
        blended_per90 = (
            season_per90 * (1.0 - recent_weight)
            + recent_per90 * recent_weight
        )

    projection = blended_per90 * safe_number(expected_minutes) / 90.0

    return {
        "season_per90": round2(season_per90),
        "recent_per90": round2(recent_per90),
        "blended_per90": round2(blended_per90),
        "recent_minutes": round2(recent_minutes),
        "recent_appearances": recent_appearances,
        "recent_weight": round2(recent_weight),
        "projection": round2(projection),
    }


def enrich_stats_with_per90(stats: dict) -> dict:
    """
    Add per-90 fields to the normalized Sportmonks totals.

    IMPORTANT:
    parse_sportmonks_statistics() returns season totals. The scanner
    consumes *_per90 fields, so those fields must be created before
    fixture_analysis_sportmonks() builds projections.
    """
    normalized = {
        key: round2(value)
        for key, value in (stats or {}).items()
    }

    minutes = safe_number(normalized.get("minutes"))

    for key in (
        "shots",
        "shots_on_target",
        "goals",
        "assists",
        "key_passes",
        "fouls_committed",
        "fouls_drawn",
        "yellow_cards",
        "red_cards",
    ):
        value = safe_number(normalized.get(key))
        normalized[f"{key}_per90"] = (
            round2(value / minutes * 90.0)
            if minutes > 0
            else 0.0
        )

    return normalized


def aggregate_recent_fixture_player_stats(fixtures: list, team_id: int) -> dict:
    """Normalize per-match player statistics from recent team fixtures.

    Only players with recorded minutes are returned. This prevents unused
    substitutes from inflating the appearance sample.
    """
    result = {}
    metric_types = {
        42: "shots",
        86: "shots_on_target",
        52: "goals",
        79: "assists",
        56: "fouls_committed",
        96: "fouls_drawn",
        84: "yellow_cards",
        119: "minutes",
    }

    for fixture in fixtures or []:
        fixture_id = fixture.get("id")
        timestamp = fixture.get("starting_at_timestamp")
        for lineup in fixture.get("lineups") or []:
            if safe_number(lineup.get("team_id")) != safe_number(team_id):
                continue

            player_id = lineup.get("player_id")
            if not player_id:
                continue

            row = {
                "fixture_id": fixture_id,
                "timestamp": timestamp,
                "minutes": 0.0,
                "shots": 0.0,
                "shots_on_target": 0.0,
                "goals": 0.0,
                "assists": 0.0,
                "fouls_committed": 0.0,
                "fouls_drawn": 0.0,
                "yellow_cards": 0.0,
                "started": lineup_type_id(lineup) == 11,
            }

            for detail in lineup.get("details") or []:
                key = metric_types.get(int(safe_number(detail.get("type_id"))))
                if key:
                    row[key] += stat_value({"value": detail.get("data", detail.get("value"))})

            # No recorded minutes means the player did not participate.
            if row["minutes"] <= 0:
                continue

            result.setdefault(player_id, []).append(row)

    for rows in result.values():
        rows.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)

    return result


def apply_recent_form_to_player(player: dict, recent_by_metric: dict) -> dict:
    """Apply recency-weighted rates to a normalized player in-place."""
    market_to_key = {
        "Remates": ("shots", "shots_per90"),
        "Remates a puerta": ("shots_on_target", "shots_on_target_per90"),
        "Goles": ("goals", "goals_per90"),
        "Asistencias": ("assists", "assists_per90"),
        "Faltas": ("fouls_committed", "fouls_committed_per90"),
        "Tarjetas": ("yellow_cards", "yellow_cards_per90"),
    }

    player.setdefault("projection", {})
    player["recent_form"] = {}

    season_stats = {
        "minutes": player.get("minutes_season", 0),
    }
    for metric, _per90 in market_to_key.values():
        season_stats[metric] = player.get(f"{metric}_season", 0)

    for market, (metric, per90_key) in market_to_key.items():
        result = blend_season_and_recent_per90(
            season_stats,
            (recent_by_metric or {}).get(metric, []),
            metric,
            player.get("expected_minutes", 0),
        )
        player[per90_key + "_blended"] = result["blended_per90"]
        player["recent_form"][market] = result
        player["projection"][market] = result["projection"]

    return player


def parse_sportmonks_statistics(statistics) -> dict:
    totals = {
        "minutes": 0.0,
        "appearances": 0.0,
        "starts": 0.0,
        "shots": 0.0,
        "shots_on_target": 0.0,
        "goals": 0.0,
        "assists": 0.0,
        "key_passes": 0.0,
        "fouls_committed": 0.0,
        "fouls_drawn": 0.0,
        "yellow_cards": 0.0,
        "red_cards": 0.0,
        "rating": 0.0,
    }

    if isinstance(statistics, dict):
        statistics = [statistics]

    for block in statistics or []:
        details = block.get("details") or block.get("statistics") or []
        if isinstance(details, dict):
            details = [details]

        for detail in details:
            type_id = safe_number(detail.get("type_id"))
            key = SPORTMONKS_STAT_TYPES.get(int(type_id))
            if key:
                totals[key] += stat_value(detail)

    return {key: round2(value) for key, value in totals.items()}


def normalize_sportmonks_participant(participant: dict) -> dict:
    meta = participant.get("meta") or {}
    return {
        "id": participant.get("id"),
        "name": participant.get("name"),
        "logo": participant.get("image_path"),
        "location": meta.get("location"),
    }


def normalize_sportmonks_fixture(item: dict) -> dict:
    participants = item.get("participants") or []

    home = next(
        (
            p for p in participants
            if (p.get("meta") or {}).get("location") == "home"
        ),
        participants[0] if participants else None,
    )

    away = next(
        (
            p for p in participants
            if (p.get("meta") or {}).get("location") == "away"
        ),
        participants[1] if len(participants) > 1 else None,
    )

    league = item.get("league") or {}
    season = item.get("season") or {}
    state = item.get("state") or {}

    return {
        "fixture_id": item.get("id"),
        "date": item.get("starting_at"),
        "timestamp": item.get("starting_at_timestamp"),
        "status": {
            "id": state.get("id"),
            "name": state.get("name"),
            "short_name": state.get("short_name"),
        },
        "league": {
            "id": item.get("league_id") or league.get("id"),
            "name": league.get("name"),
            "country": (league.get("country") or {}).get("name"),
        },
        "season": {
            "id": item.get("season_id") or season.get("id"),
            "name": season.get("name"),
        },
        "home": normalize_sportmonks_participant(home or {}),
        "away": normalize_sportmonks_participant(away or {}),
    }


def lineup_type_id(entry: dict) -> int:
    return int(safe_number(entry.get("type_id")))


def normalize_sportmonks_lineups(fixture_item: dict) -> list:
    """
    Normalize Sportmonks lineups and resolve team metadata from fixture
    participants when lineup rows omit team_name/team.
    """
    raw = fixture_item.get("lineups") or []
    grouped = {}

    participant_by_id = {}
    for participant in fixture_item.get("participants") or []:
        participant_id = participant.get("id")
        if participant_id is not None:
            participant_by_id[str(participant_id)] = participant

    for entry in raw:
        team_id = entry.get("team_id")
        if team_id is None:
            team_obj = entry.get("team") or {}
            team_id = team_obj.get("id")
        if team_id is None:
            continue

        participant = participant_by_id.get(str(team_id), {})
        team_obj = entry.get("team") or {}
        team_name = (
            entry.get("team_name")
            or team_obj.get("name")
            or participant.get("name")
            or "Equipo"
        )
        team_logo = (
            entry.get("team_logo")
            or team_obj.get("image_path")
            or participant.get("image_path")
        )

        group = grouped.setdefault(
            team_id,
            {
                "team": {
                    "id": team_id,
                    "name": team_name,
                    "logo": team_logo,
                },
                "formation": None,
                "starters": [],
                "substitutes": [],
            },
        )

        if group["team"].get("name") in (None, "", "Equipo"):
            group["team"]["name"] = team_name
        if not group["team"].get("logo"):
            group["team"]["logo"] = team_logo

        player_obj = entry.get("player") or {}
        player = {
            "player_id": entry.get("player_id") or player_obj.get("id"),
            "name": entry.get("player_name") or player_obj.get("name"),
            "number": entry.get("jersey_number"),
            "position_id": entry.get("position_id"),
            "formation_field": entry.get("formation_field"),
            "formation_position": entry.get("formation_position"),
            "starter": lineup_type_id(entry) == 11,
            "substitute": lineup_type_id(entry) == 12,
        }

        if player["starter"]:
            group["starters"].append(player)
        elif player["substitute"]:
            group["substitutes"].append(player)

    return list(grouped.values())


def normalize_player_from_sportmonks(item: dict, stats: dict) -> dict:
    minutes = stats.get("minutes", 0.0)
    appearances = stats.get("appearances", 0.0)

    def per90(value):
        if minutes <= 0:
            return 0.0
        return round2(value / minutes * 90.0)

    return {
        "player_id": item.get("id"),
        "player": (
            item.get("name")
            or item.get("display_name")
            or "Jugador"
        ),
        "photo": item.get("image_path"),
        "age": item.get("age"),
        **stats,
        "shots_per90": per90(stats.get("shots", 0)),
        "shots_on_target_per90": per90(stats.get("shots_on_target", 0)),
        "goals_per90": per90(stats.get("goals", 0)),
        "assists_per90": per90(stats.get("assists", 0)),
        "fouls_committed_per90": per90(stats.get("fouls_committed", 0)),
        "fouls_drawn_per90": per90(stats.get("fouls_drawn", 0)),
        "yellow_cards_per90": per90(stats.get("yellow_cards", 0)),
        "key_passes_per90": per90(stats.get("key_passes", 0)),
    }


# ============================================================
# SPORTMONKS FIXTURE HELPERS
# ============================================================

async def sportmonks_fixture(fixture_id: int, include: str = "") -> dict:
    params = {}
    if include:
        params["include"] = include

    return await sportmonks_get(
        f"/football/fixtures/{fixture_id}",
        params,
    )


async def get_sportmonks_fixture_detail(fixture_id: int) -> dict:
    return await sportmonks_fixture(
        fixture_id,
        "participants;league;season;state;lineups;formations",
    )


def parse_sportmonks_season_statistics_response(payload: dict) -> dict:
    """Parse the dedicated Sportmonks season-statistics response.

    Sportmonks exposes player season statistics at
    /football/statistics/seasons/{player}/{season}. Each season statistic
    record contains a `details` list with type_id/value pairs.
    """
    data = (payload or {}).get("data") or []
    if isinstance(data, dict):
        data = [data]

    totals = parse_sportmonks_statistics(data)
    return enrich_stats_with_per90(totals)


async def get_sportmonks_player_season_statistics(
    player_id: int,
    season_id: int,
) -> dict:
    """Retrieve season statistics through Sportmonks' dedicated endpoint."""
    return await sportmonks_get(
        f"/football/statistics/seasons/{player_id}/{season_id}",
    )


async def get_sportmonks_player_season(
    player_id: int,
    season_id: Optional[int] = None,
) -> dict:
    """Get a player and defensively keep only the requested season."""
    include = "statistics.details.type"
    params = {"include": include}

    result = await sportmonks_get(
        f"/football/players/{player_id}",
        params,
    )

    if not result.get("ok") or season_id is None:
        return result

    payload = result.get("data") or {}
    player = payload.get("data") or {}
    statistics = player.get("statistics") or []

    selected = []
    for stat in statistics:
        season = stat.get("season") or {}
        sid = (
            stat.get("season_id")
            or season.get("id")
            or (season if isinstance(season, int) else None)
        )
        if sid is None:
            continue
        try:
            if int(sid) == int(season_id):
                selected.append(stat)
        except (TypeError, ValueError):
            continue

    player["statistics"] = selected
    payload["data"] = player
    result["data"] = payload
    return result


def fixture_is_completed(fixture: dict) -> bool:
    """Return True only for completed fixtures when Sportmonks exposes state.

    If a state is absent, existing lineups remain the fallback evidence that
    the fixture was played; if a state is present, explicit non-finished
    statuses are rejected.
    """
    state = fixture.get("state") or {}
    if not isinstance(state, dict) or not state:
        return bool(fixture.get("lineups"))

    raw_values = [
        state.get("short_name"),
        state.get("name"),
        state.get("state"),
        state.get("developer_name"),
    ]
    values = {str(v).strip().lower() for v in raw_values if v is not None}
    if not values:
        return bool(fixture.get("lineups"))

    finished = {
        "ft", "finished", "aet", "pen", "after extra time",
        "after penalties", "full time", "ended", "complete", "completed",
    }
    unfinished = {
        "ns", "not started", "scheduled", "1h", "2h", "ht", "live",
        "inplay", "postponed", "cancelled", "canceled", "abandoned",
    }

    if values & finished:
        return True
    if values & unfinished:
        return False
    # Unknown explicit state: require lineups rather than assuming completion.
    return bool(fixture.get("lineups"))


async def get_recent_team_fixtures(
    team_id: int,
    season_id: int,
    before_timestamp: Optional[int],
) -> list:
    """Fetch the most recent completed fixtures for one team."""
    if not team_id or not season_id:
        return []

    if before_timestamp:
        end_dt = datetime.fromtimestamp(
            int(before_timestamp),
            tz=timezone.utc,
        ) - timedelta(days=1)
    else:
        end_dt = datetime.now(timezone.utc) - timedelta(days=1)

    start_dt = end_dt - timedelta(days=RECENT_FORM_LOOKBACK_DAYS)
    cache_key = (
        int(team_id),
        int(season_id),
        end_dt.date().isoformat(),
    )
    now = datetime.now(timezone.utc).timestamp()
    cached = RECENT_FORM_CACHE.get(cache_key)
    if cached and now - cached["timestamp"] < RECENT_FORM_CACHE_TTL:
        return list(cached["fixtures"])

    # Sportmonks V3 team date-range endpoint is:
    # /football/fixtures/between/{start_date}/{end_date}/{team_id}
    # (there is NO /date/ segment on this endpoint).
    endpoint = (
        f"/football/fixtures/between/"
        f"{start_dt.date().isoformat()}/"
        f"{end_dt.date().isoformat()}/"
        f"{int(team_id)}"
    )
    params = {
        # Include team metadata plus per-player match details.
        "include": "participants;state;lineups.details.type",
        "order": "desc",
        "per_page": min(max(RECENT_FORM_MATCHES * 2, 12), 50),
    }

    result = await sportmonks_get(endpoint, params)
    if not result.get("ok"):
        return []

    payload = result.get("data") or {}
    fixtures = payload.get("data") or [] if isinstance(payload, dict) else []
    if isinstance(fixtures, dict):
        fixtures = [fixtures]

    completed = []
    for fixture in fixtures:
        ts = safe_number(fixture.get("starting_at_timestamp"))
        if before_timestamp and ts >= safe_number(before_timestamp):
            continue
        # Recent form must be chronological, not locked to the current
        # season. At the start of a new season, the latest matches may belong
        # to the previous Sportmonks season.
        if not fixture_is_completed(fixture):
            continue
        completed.append(fixture)

    completed.sort(
        key=lambda x: safe_number(x.get("starting_at_timestamp")),
        reverse=True,
    )
    completed = completed[:RECENT_FORM_MATCHES]

    RECENT_FORM_CACHE[cache_key] = {
        "timestamp": now,
        "fixtures": list(completed),
    }
    return completed


async def get_recent_player_form(
    team_id: int,
    season_id: int,
    before_timestamp: Optional[int],
) -> dict:
    fixtures = await get_recent_team_fixtures(
        team_id,
        season_id,
        before_timestamp,
    )
    per_player = aggregate_recent_fixture_player_stats(
        fixtures,
        team_id,
    )

    metric_map = {
        "shots": "shots",
        "shots_on_target": "shots_on_target",
        "goals": "goals",
        "assists": "assists",
        "fouls_committed": "fouls_committed",
        "yellow_cards": "yellow_cards",
    }
    result = {}
    for player_id, rows in per_player.items():
        result[player_id] = {
            key: [
                {
                    "minutes": row.get("minutes", 0),
                    key: row.get(key, 0),
                    "started": row.get("started", False),
                }
                for row in rows
            ]
            for key in metric_map.values()
        }
    return result


async def collect_sportmonks_player_stats(
    player_id: int,
    season_id: Optional[int],
) -> dict:
    cache_key = (int(player_id), int(season_id) if season_id is not None else None)
    now = datetime.now(timezone.utc).timestamp()
    cached = PLAYER_STATS_CACHE.get(cache_key)
    if cached and now - cached["timestamp"] < PLAYER_STATS_CACHE_TTL:
        return dict(cached["stats"])

    stats = {}

    # Primary path: dedicated season-statistics endpoint. Sportmonks documents
    # this endpoint as returning player season records with a `details` list,
    # which avoids ambiguity in the generic /players/{id}?include=statistics
    # response.
    if season_id is not None:
        season_result = await get_sportmonks_player_season_statistics(
            player_id,
            int(season_id),
        )
        if season_result.get("ok"):
            stats = parse_sportmonks_season_statistics_response(
                season_result.get("data") or {}
            )

    # Compatibility fallback: keep the existing player endpoint available for
    # leagues/plans where the dedicated season endpoint is unavailable.
    if not stats or not any(
        safe_number(stats.get(key)) > 0
        for key in (
            "minutes",
            "appearances",
            "starts",
            "shots",
            "shots_on_target",
            "goals",
            "assists",
            "fouls_committed",
            "yellow_cards",
        )
    ):
        result = await get_sportmonks_player_season(player_id, season_id)
        if result.get("ok"):
            item = result.get("data", {}).get("data") or {}
            fallback_stats = parse_sportmonks_statistics(
                item.get("statistics") or []
            )
            fallback_stats = enrich_stats_with_per90(fallback_stats)
            if any(
                safe_number(fallback_stats.get(key)) > 0
                for key in (
                    "minutes",
                    "appearances",
                    "starts",
                    "shots",
                    "shots_on_target",
                    "goals",
                    "assists",
                    "fouls_committed",
                    "yellow_cards",
                )
            ):
                stats = fallback_stats

    PLAYER_STATS_CACHE[cache_key] = {
        "timestamp": now,
        "stats": dict(stats),
    }
    return stats


# ============================================================
# ROOT / HEALTH
# ============================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "project": "Fútbol Analytics",
        "version": APP_VERSION,
        "engine": "Player Market Scanner V2",
        "primary_source": "Sportmonks",
        "sportmonks_configured": bool(SPORTMONKS_TOKEN),
        "api_football_fallback_configured": bool(APIFOOTBALL_KEY),
        "features": [
            "seleccion_de_partido",
            "alineaciones",
            "estadisticas_temporada",
            "proyecciones",
            "player_market_scanner",
            "bankroll",
            "bet_tracker",
        ],
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "sportmonks_configured": bool(SPORTMONKS_TOKEN),
        "api_football_configured": bool(APIFOOTBALL_KEY),
    }


# ============================================================
# TEST SPORTMONKS
# ============================================================

@app.get("/api/test-sportmonks")
async def test_sportmonks():
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    result = await sportmonks_get(
        f"/football/fixtures/date/{date}",
        {"include": "participants"},
    )

    if not result["ok"]:
        return {
            "status": "error",
            "api_configured": bool(SPORTMONKS_TOKEN),
            "status_code": result["status_code"],
            "message": result["error"],
            "details": result["data"],
        }

    fixtures = result["data"].get("data") or []

    return {
        "status": "connected",
        "api_configured": True,
        "status_code": result["status_code"],
        "date_tested": date,
        "count": len(fixtures),
        "source": "Sportmonks",
        "sample_fixture": (
            normalize_sportmonks_fixture(fixtures[0])
            if fixtures else None
        ),
    }


# ============================================================
# TEST API-FOOTBALL
# ============================================================

@app.get("/api/test-apifootball")
async def test_apifootball():
    result = await apifootball_get("/status")

    if not result["ok"]:
        return {
            "status": "error",
            "api_configured": bool(APIFOOTBALL_KEY),
            "status_code": result["status_code"],
            "message": result["error"],
            "details": result["data"],
        }

    data = result["data"]
    account = data.get("response", {}).get("account", {})
    subscription = data.get("response", {}).get("subscription", {})
    requests_info = data.get("response", {}).get("requests", {})

    return {
        "status": "connected",
        "api_configured": True,
        "status_code": result["status_code"],
        "account": {
            "firstname": account.get("firstname"),
            "lastname": account.get("lastname"),
            "email": account.get("email"),
        },
        "subscription": subscription,
        "requests": requests_info,
    }


# ============================================================
# FIXTURES - SPORTMONKS PRINCIPAL
# ============================================================

@app.get("/api/sportmonks-fixtures")
async def sportmonks_fixtures(
    date: Optional[str] = None,
):
    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    result = await sportmonks_get(
        f"/football/fixtures/date/{target_date}",
        {
            "include": "participants;league;season;state",
        },
    )

    if not result["ok"]:
        return {
            "status": "error",
            "count": 0,
            "fixtures": [],
            "source": "Sportmonks",
            "message": result["error"],
            "details": result["data"],
        }

    raw = result["data"].get("data") or []
    fixtures = [normalize_sportmonks_fixture(x) for x in raw]

    fixtures.sort(
        key=lambda x: x.get("timestamp") or 0
    )

    return {
        "status": "ok",
        "count": len(fixtures),
        "date": target_date,
        "fixtures": fixtures,
        "source": "Sportmonks",
    }


# ============================================================
# FIXTURES - API FOOTBALL FALLBACK
# ============================================================

@app.get("/api/apifootball-fixtures")
async def apifootball_fixtures(
    date: Optional[str] = None,
    league: Optional[int] = None,
    season: int = CURRENT_SEASON_YEAR,
    next: int = 100,
):
    params = {}

    if date:
        params["date"] = date
    elif league:
        params["league"] = league
        params["season"] = season
        params["next"] = min(max(next, 1), 100)
    else:
        params["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    result = await apifootball_get("/fixtures", params)

    if not result["ok"]:
        return {
            "status": "error",
            "count": 0,
            "fixtures": [],
            "message": result["error"],
            "details": result["data"],
        }

    fixtures = []

    for item in result["data"].get("response", []):
        fixture = item.get("fixture") or {}
        teams = item.get("teams") or {}
        league_info = item.get("league") or {}
        status = fixture.get("status") or {}

        fixtures.append({
            "fixture_id": fixture.get("id"),
            "date": fixture.get("date"),
            "timestamp": fixture.get("timestamp"),
            "status": status,
            "league": league_info,
            "home": teams.get("home"),
            "away": teams.get("away"),
        })

    return {
        "status": "ok",
        "count": len(fixtures),
        "fixtures": fixtures,
        "source": "API-Football",
        "season": season,
    }


# ============================================================
# FIXTURE DETAIL - SPORTMONKS
# ============================================================

@app.get("/api/fixture")
async def fixture_detail(fixture_id: int):
    result = await get_sportmonks_fixture_detail(fixture_id)

    if result["ok"]:
        item = result["data"].get("data") or {}
        return {
            "status": "ok",
            "fixture": normalize_sportmonks_fixture(item),
            "raw": item,
            "source": "Sportmonks",
        }

    # Fallback
    fallback = await apifootball_get(
        "/fixtures",
        {"id": fixture_id},
    )

    if not fallback["ok"]:
        return {
            "status": "error",
            "fixture_id": fixture_id,
            "message": result["error"],
            "details": result["data"],
            "fallback": fallback["error"],
        }

    response = fallback["data"].get("response", [])
    if not response:
        return {
            "status": "not_found",
            "fixture_id": fixture_id,
            "message": "Fixture no encontrado.",
        }

    item = response[0]
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    league = item.get("league") or {}

    return {
        "status": "ok",
        "fixture": {
            "fixture_id": fixture.get("id"),
            "date": fixture.get("date"),
            "timestamp": fixture.get("timestamp"),
            "status": fixture.get("status"),
            "league": league,
            "home": teams.get("home"),
            "away": teams.get("away"),
        },
        "source": "API-Football-fallback",
    }


# ============================================================
# LINEUPS - SPORTMONKS
# ============================================================

@app.get("/api/fixture-lineups")
async def fixture_lineups(fixture_id: int):
    result = await get_sportmonks_fixture_detail(fixture_id)

    if result["ok"]:
        item = result["data"].get("data") or {}
        lineups = normalize_sportmonks_lineups(item)

        if lineups:
            return {
                "status": "confirmed_lineups",
                "available": True,
                "fixture_id": fixture_id,
                "count": len(lineups),
                "lineups": lineups,
                "source": "Sportmonks",
            }

    # Fallback
    fallback = await apifootball_get(
        "/fixtures/lineups",
        {"fixture": fixture_id},
    )

    if not fallback["ok"]:
        return {
            "status": "lineups_pending",
            "fixture_id": fixture_id,
            "available": False,
            "lineups": [],
            "message": result["error"],
            "fallback": fallback["error"],
        }

    response = fallback["data"].get("response", [])

    if not response:
        return {
            "status": "lineups_pending",
            "fixture_id": fixture_id,
            "available": False,
            "lineups": [],
            "message": "Las alineaciones todavía no están disponibles.",
        }

    lineups = []

    for block in response:
        team = block.get("team") or {}
        coach = block.get("coach") or {}

        starters = []
        substitutes = []

        for entry in block.get("startXI", []) or []:
            player = entry.get("player") or {}
            starters.append({
                "player_id": player.get("id"),
                "name": player.get("name"),
                "number": player.get("number"),
                "position": player.get("pos"),
                "starter": True,
            })

        for entry in block.get("substitutes", []) or []:
            player = entry.get("player") or {}
            substitutes.append({
                "player_id": player.get("id"),
                "name": player.get("name"),
                "number": player.get("number"),
                "position": player.get("pos"),
                "starter": False,
            })

        lineups.append({
            "team": team,
            "coach": coach,
            "formation": block.get("formation"),
            "starters": starters,
            "substitutes": substitutes,
            "starter_count": len(starters),
            "substitute_count": len(substitutes),
        })

    return {
        "status": "confirmed_lineups",
        "available": True,
        "fixture_id": fixture_id,
        "count": len(lineups),
        "lineups": lineups,
        "source": "API-Football-fallback",
    }


# ============================================================
# PLAYER SEASON - SPORTMONKS
# ============================================================

@app.get("/api/player-season")
async def player_season(
    player_id: int,
    season: Optional[int] = None,
):
    requested_season = season or SPORTMONKS_DEFAULT_SEASON_ID

    result = await get_sportmonks_player_season(
        player_id,
        requested_season,
    )

    if result["ok"]:
        item = result["data"].get("data") or {}
        stats = parse_sportmonks_statistics(
            item.get("statistics") or []
        )

        normalized = normalize_player_from_sportmonks(
            item,
            stats,
        )

        return {
            "status": "ok",
            "season": requested_season,
            "player": normalized,
            "source": "Sportmonks",
        }

    # Fallback
    fallback = await apifootball_get(
        "/players",
        {
            "id": player_id,
            "season": api_football_season_year(season),
        },
    )

    if not fallback["ok"]:
        return {
            "status": "error",
            "player_id": player_id,
            "statistics": [],
            "message": result["error"],
            "fallback": fallback["error"],
        }

    response = fallback["data"].get("response", [])

    if not response:
        return {
            "status": "no_data",
            "player_id": player_id,
            "statistics": [],
            "message": "No existen estadísticas para este jugador.",
        }

    item = response[0]
    player = item.get("player") or {}
    stats = {}

    for block in item.get("statistics") or []:
        games = block.get("games") or {}
        shots = block.get("shots") or {}
        goals = block.get("goals") or {}
        fouls = block.get("fouls") or {}
        cards = block.get("cards") or {}

        stats["minutes"] = safe_number(games.get("minutes"))
        stats["appearances"] = safe_number(games.get("appearences"))
        stats["starts"] = safe_number(games.get("lineups"))
        stats["shots"] = safe_number(shots.get("total"))
        stats["shots_on_target"] = safe_number(shots.get("on"))
        stats["goals"] = safe_number(goals.get("total"))
        stats["assists"] = safe_number(goals.get("assists"))
        stats["fouls_committed"] = safe_number(fouls.get("committed"))
        stats["fouls_drawn"] = safe_number(fouls.get("drawn"))
        stats["yellow_cards"] = safe_number(cards.get("yellow"))

    minutes = stats.get("minutes", 0)

    def per90(value):
        return round2(value / minutes * 90) if minutes else 0.0

    normalized = {
        "player_id": player.get("id"),
        "player": player.get("name"),
        "photo": player.get("photo"),
        **{k: round2(v) for k, v in stats.items()},
        "shots_per90": per90(stats.get("shots", 0)),
        "shots_on_target_per90": per90(stats.get("shots_on_target", 0)),
        "goals_per90": per90(stats.get("goals", 0)),
        "assists_per90": per90(stats.get("assists", 0)),
        "fouls_committed_per90": per90(stats.get("fouls_committed", 0)),
        "fouls_drawn_per90": per90(stats.get("fouls_drawn", 0)),
        "yellow_cards_per90": per90(stats.get("yellow_cards", 0)),
    }

    return {
        "status": "ok",
        "season": api_football_season_year(season),
        "player": normalized,
        "source": "API-Football-fallback",
    }


# ============================================================
# FIXTURE ANALYSIS - SPORTMONKS
# ============================================================

async def fixture_analysis_sportmonks(
    fixture_id: int,
    season: Optional[int] = None,
):
    result = await sportmonks_fixture(
        fixture_id,
        (
            "participants;league;season;state;"
            "lineups.details.type;formations"
        ),
    )

    if not result["ok"]:
        return None, result

    fixture_item = result["data"].get("data") or {}
    lineups = normalize_sportmonks_lineups(fixture_item)

    if not lineups:
        return {
            "status": "lineups_pending",
            "available": False,
            "fixture_id": fixture_id,
            "players": [],
            "message": (
                "Sportmonks no ha entregado alineaciones "
                "para este fixture todavía."
            ),
            "source": "Sportmonks",
        }, None

    season_id = (
        safe_number(fixture_item.get("season_id"))
        or safe_number((fixture_item.get("season") or {}).get("id"))
        or safe_number(season)
        or None
    )

    players = []

    # Keep team metadata attached even if a lineup row arrives without a
    # populated team object. Sportmonks fixture participants are the source
    # of truth for the home/away team names.
    participant_by_id = {
        str(p.get("id")): p
        for p in (fixture_item.get("participants") or [])
        if p.get("id") is not None
    }

    for lineup in lineups:
        team = lineup.get("team") or {}
        team_id = team.get("id")
        participant = participant_by_id.get(str(team_id), {})
        team_name = (
            team.get("name")
            or participant.get("name")
            or "Equipo"
        )

        for entry in (
            lineup.get("starters", [])
            + lineup.get("substitutes", [])
        ):
            pid = entry.get("player_id")
            if not pid:
                continue

            stats = await collect_sportmonks_player_stats(
                pid,
                int(season_id) if season_id else None,
            )

            starter = bool(entry.get("starter"))

            minutes = safe_number(stats.get("minutes"))
            appearances = safe_number(stats.get("appearances"))
            starts = safe_number(stats.get("starts"))

            if starter:
                expected_minutes = 75.0
                if appearances > 0:
                    start_rate = starts / appearances
                    if start_rate >= 0.75:
                        expected_minutes = 80.0
                    elif start_rate >= 0.50:
                        expected_minutes = 77.0
            else:
                expected_minutes = 20.0

            def project(key):
                return round2(
                    safe_number(stats.get(key + "_per90"))
                    * expected_minutes / 90.0
                )

            stats_available = (
                minutes > 0
                or appearances > 0
                or starts > 0
                or any(
                    safe_number(stats.get(key)) > 0
                    for key in (
                        "shots",
                        "shots_on_target",
                        "goals",
                        "assists",
                        "fouls_committed",
                        "yellow_cards",
                    )
                )
            )

            data_quality, data_quality_reason = classify_data_quality(
                minutes,
                appearances,
            )

            confidence = calculate_data_confidence(
                minutes=minutes,
                appearances=appearances,
                starts=starts,
                starter=starter,
            )

            players.append({
                "player_id": pid,
                "player": entry.get("name"),
                "team": team_name,
                "team_id": team_id,
                "number": entry.get("number"),
                "position_id": entry.get("position_id"),
                "formation_field": entry.get("formation_field"),
                "starter": starter,
                "confirmed_lineup": True,
                "expected_minutes": expected_minutes,
                "stats_available": stats_available,
                "data_quality": data_quality,
                "data_quality_reason": data_quality_reason,
                "confidence": confidence,

                "minutes_season": minutes,
                "appearances_season": appearances,
                "starts_season": starts,

                "shots_season": stats.get("shots", 0),
                "shots_on_target_season": stats.get("shots_on_target", 0),
                "goals_season": stats.get("goals", 0),
                "assists_season": stats.get("assists", 0),
                "fouls_committed_season": stats.get("fouls_committed", 0),
                "fouls_drawn_season": stats.get("fouls_drawn", 0),
                "yellow_cards_season": stats.get("yellow_cards", 0),

                "shots_per90": stats.get("shots_per90", 0),
                "shots_on_target_per90": stats.get("shots_on_target_per90", 0),
                "goals_per90": stats.get("goals_per90", 0),
                "assists_per90": stats.get("assists_per90", 0),
                "fouls_committed_per90": stats.get("fouls_committed_per90", 0),
                "fouls_drawn_per90": stats.get("fouls_drawn_per90", 0),
                "yellow_cards_per90": stats.get("yellow_cards_per90", 0),

                "projection": {
                    "Remates": project("shots"),
                    "Remates a puerta": project("shots_on_target"),
                    "Goles": project("goals"),
                    "Asistencias": project("assists"),
                    "Faltas": project("fouls_committed"),
                    "Tarjetas": project("yellow_cards"),
                },
            })

    # Recency layer: fetch the latest completed team fixtures once per team
    # and reuse them for every player in the current lineup. This avoids the
    # previous problem where projections were based almost entirely on a tiny
    # season sample such as 1-2 appearances.
    before_timestamp = fixture_item.get("starting_at_timestamp")
    team_ids = sorted({
        int(p["team_id"])
        for p in players
        if p.get("team_id") is not None
    })
    recent_tasks = [
        get_recent_player_form(
            team_id,
            int(season_id),
            int(before_timestamp) if before_timestamp else None,
        )
        for team_id in team_ids
    ]
    recent_results = (
        await asyncio.gather(*recent_tasks)
        if recent_tasks else []
    )
    recent_by_team = dict(zip(team_ids, recent_results))

    for player in players:
        team_form = recent_by_team.get(int(player["team_id"])) if player.get("team_id") is not None else {}
        recent_by_metric = (team_form or {}).get(player.get("player_id"), {})
        apply_recent_form_to_player(player, recent_by_metric)

        player["recent_form_available"] = bool(recent_by_metric)
        player["recent_form_matches"] = max(
            [
                len(value)
                for value in (recent_by_metric or {}).values()
            ] or [0]
        )
        player["recent_form_minutes"] = safe_number(
            ((player.get("recent_form") or {}).get("Remates") or {}).get("recent_minutes")
        )
        player["recent_form_weight"] = safe_number(
            ((player.get("recent_form") or {}).get("Remates") or {}).get("recent_weight")
        )
        recent_matches = player["recent_form_matches"]
        player["confidence"] = calculate_data_confidence(
            minutes=player.get("minutes_season", 0),
            appearances=player.get("appearances_season", 0),
            starts=player.get("starts_season", 0),
            starter=bool(player.get("starter")),
            recent_matches=recent_matches,
        )
        player["recent_form_sample_quality"] = (
            "SOLIDA" if recent_matches >= 5
            else "MEDIA" if recent_matches >= 3
            else "BAJA" if recent_matches >= 1
            else "SIN MUESTRA"
        )
        player["projection_method"] = (
            "temporada + forma reciente"
            if recent_by_metric
            else "temporada"
        )

    fixture = normalize_sportmonks_fixture(fixture_item)

    starters = [p for p in players if p["starter"]]
    substitutes = [p for p in players if not p["starter"]]
    players_with_stats = [p for p in players if p["stats_available"]]
    players_with_projection = [
        p for p in players
        if any(
            safe_number(v) > 0
            for v in (p.get("projection") or {}).values()
        )
    ]

    return {
        "status": "real_player_data",
        "available": True,
        "fixture_id": fixture_id,
        "season": season_id,
        "fixture": fixture,
        "lineups_confirmed": True,
        "count": len(players),
        "starter_count": len(starters),
        "substitute_count": len(substitutes),
        "players_with_stats": len(players_with_stats),
        "players_with_projection": len(players_with_projection),
        "players": players,
        "markets": SUPPORTED_MARKETS,
        "data_quality": (
            "Alineaciones y estadísticas obtenidas desde Sportmonks. "
            "Las proyecciones usan estadísticas de temporada y "
            "minutos esperados; no incluyen cuotas de bookmaker."
        ),
        "source": "Sportmonks",
        "motor": "Fútbol Analytics V2.2",
    }, None


# ============================================================
# FIXTURE ANALYSIS - FALLBACK API FOOTBALL
# ============================================================

@app.get("/api/fixture-analysis")
async def fixture_analysis(
    fixture_id: int,
    season: Optional[int] = None,
):
    primary, error = await fixture_analysis_sportmonks(
        fixture_id,
        season,
    )

    # Only return the primary result when it actually has usable data.
    # If Sportmonks reports lineups pending, continue to API-Football.
    if primary is not None and primary.get("available"):
        return primary

    # Si Sportmonks falla, usamos API-Football como respaldo.
    fixture_result = await apifootball_get(
        "/fixtures",
        {"id": fixture_id},
    )

    if not fixture_result["ok"]:
        return {
            "status": "error",
            "fixture_id": fixture_id,
            "message": error["error"] if error else "Error",
            "details": error["data"] if error else None,
            "fallback": fixture_result["error"],
        }

    fixtures = fixture_result["data"].get("response", [])

    if not fixtures:
        return {
            "status": "not_found",
            "fixture_id": fixture_id,
            "message": "Fixture no encontrado.",
        }

    item = fixtures[0]
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    league = item.get("league") or {}

    lineup_result = await apifootball_get(
        "/fixtures/lineups",
        {"fixture": fixture_id},
    )

    if not lineup_result["ok"]:
        return {
            "status": "lineups_pending",
            "available": False,
            "fixture_id": fixture_id,
            "players": [],
            "message": "No fue posible obtener las alineaciones.",
            "details": lineup_result["data"],
        }

    lineup_response = lineup_result["data"].get("response", [])

    if not lineup_response:
        return {
            "status": "lineups_pending",
            "available": False,
            "fixture_id": fixture_id,
            "players": [],
            "message": "Las alineaciones todavía no están disponibles.",
        }

    players = []

    for block in lineup_response:
        team = block.get("team") or {}

        for entry in (
            list(block.get("startXI", []) or [])
            + list(block.get("substitutes", []) or [])
        ):
            p = entry.get("player") or {}
            pid = p.get("id")
            if not pid:
                continue

            starter = entry in (block.get("startXI", []) or [])

            # Fallback conserva el formato V1.9, pero sin hacer
            # múltiples llamadas innecesarias por jugador.
            players.append({
                "player_id": pid,
                "player": p.get("name"),
                "team": team.get("name"),
                "team_id": team.get("id"),
                "number": p.get("number"),
                "position": p.get("pos"),
                "starter": starter,
                "confirmed_lineup": True,
                "expected_minutes": 75 if starter else 20,
                "stats_available": False,
                "data_quality": "BAJA",
                "confidence": 55 if starter else 35,
                "projection": {
                    market: 0.0 for market in SUPPORTED_MARKETS
                },
            })

    return {
        "status": "lineups_only",
        "available": True,
        "fixture_id": fixture_id,
        "season": api_football_season_year(season),
        "fixture": {
            "date": fixture.get("date"),
            "status": fixture.get("status"),
            "league": league,
            "home": teams.get("home"),
            "away": teams.get("away"),
        },
        "lineups_confirmed": True,
        "count": len(players),
        "starter_count": sum(1 for p in players if p["starter"]),
        "substitute_count": sum(1 for p in players if not p["starter"]),
        "players": players,
        "markets": SUPPORTED_MARKETS,
        "source": "API-Football-fallback",
        "motor": "Fútbol Analytics V2.2",
    }


# ============================================================
# PLAYER MARKET SCANNER V2
# ============================================================

MARKET_KEYS = {
    "Remates": "shots_per90",
    "Remates a puerta": "shots_on_target_per90",
    "Goles": "goals_per90",
    "Asistencias": "assists_per90",
    "Faltas": "fouls_committed_per90",
    "Tarjetas": "yellow_cards_per90",
}


def model_probability_for_side(
    line: float,
    projection: float,
    side: str,
) -> float:
    if side == "over":
        return probability_over(line, projection)
    return probability_under(line, projection)


def build_market_candidates(players: list, limit: int = 6) -> list:
    candidates = []

    for player in players or []:
        if not player.get("starter"):
            continue

        for market, key in MARKET_KEYS.items():
            projection = safe_number(
                player.get("projection", {}).get(market)
            )

            if projection < 0.10:
                continue

            # Reference lines bracket the projection instead of always placing
            # the over line above it.
            over_line = max(0.5, math.floor(projection) - 0.5)
            under_line = max(0.5, math.ceil(projection) + 0.5)

            over_prob = model_probability_for_side(
                over_line,
                projection,
                "over",
            )
            under_prob = model_probability_for_side(
                under_line,
                projection,
                "under",
            )

            confidence = safe_number(
                player.get("confidence")
            ) or 50

            best_side = (
                "over" if over_prob >= under_prob
                else "under"
            )
            best_line = (
                over_line if best_side == "over"
                else under_line
            )
            best_prob = max(over_prob, under_prob)

            raw_rating = round(
                max(
                    0,
                    min(
                        100,
                        best_prob * 0.62
                        + confidence * 0.28
                        + 10,
                    ),
                )
            )

            if not is_publishable_confidence(confidence):
                continue

            rating = adjust_rating_for_data_quality(
                raw_rating,
                confidence,
            )

            candidates.append({
                "player_id": player.get("player_id"),
                "player": player.get("player"),
                "team": player.get("team"),
                "team_id": player.get("team_id"),
                "market": market,
                "projection": round2(projection),
                "expected_minutes": round2(player.get("expected_minutes")),
                "projection_basis": {
                    "per90": round2(player.get(key)),
                    "blended_per90": round2(player.get(key + "_blended")) or round2(player.get(key)),
                    "expected_minutes": round2(player.get("expected_minutes")),
                    "season_minutes": round2(player.get("minutes_season")),
                    "season_appearances": round2(player.get("appearances_season")),
                    "recent_form_matches": player.get("recent_form_matches", 0),
                    "recent_form_minutes": round2(player.get("recent_form_minutes")),
                    "recent_form_weight": round2(player.get("recent_form_weight")),
                    "recent_form_sample_quality": player.get("recent_form_sample_quality", "SIN MUESTRA"),
                    "recent_form_available": bool(player.get("recent_form_available")),
                    "method": player.get("projection_method", "temporada"),
                },
                "reference_side": best_side,
                "reference_line": round2(best_line),
                "probability_fa": round2(best_prob),
                "fa_rating": rating,
                "raw_fa_rating": raw_rating,
                "confidence": round2(confidence),
                "confidence_band": confidence_band(confidence),
                "publishable": is_publishable_confidence(confidence),
                "data_quality": player.get(
                    "data_quality",
                    "BAJA",
                ),
                "data_quality_reason": player.get(
                    "data_quality_reason",
                    "No disponible",
                ),
                "projection_method": player.get(
                    "projection_method",
                    "temporada",
                ),
                "recent_form_matches": player.get("recent_form_matches", 0),
                "confirmed_lineup": bool(
                    player.get("confirmed_lineup")
                ),
            })

    candidates.sort(
        key=lambda x: (
            x["fa_rating"],
            x["probability_fa"],
            x["projection"],
        ),
        reverse=True,
    )

    return candidates[:max(1, min(limit, 20))]


@app.get("/api/player-market-scan")
async def player_market_scan(
    fixture_id: int,
    season: int = SPORTMONKS_DEFAULT_SEASON_ID,
    limit: int = 6,
):
    analysis = await fixture_analysis(
        fixture_id=fixture_id,
        season=season,
    )

    if not analysis.get("available"):
        return analysis

    candidates = build_market_candidates(
        analysis.get("players", []),
        limit=limit,
    )

    return {
        "status": "scanner_ready" if candidates else "scanner_empty",
        "fixture_id": fixture_id,
        "season": season,
        "count": len(candidates),
        "players_analyzed": len(analysis.get("players", [])),
        "players_with_stats": analysis.get("players_with_stats", 0),
        "players_with_projection": analysis.get("players_with_projection", 0),
        "players_publishable": len(candidates),
        "candidates": candidates,
        "note": (
            "Las líneas mostradas son referencias matemáticas. "
            "No representan cuotas ni líneas de bookmaker."
        ),
        "source": analysis.get("source"),
        "motor": "Fútbol Analytics V2.2",
        "confidence_policy": {
            "alta": "85-100",
            "media": "70-84",
            "precaucion": "60-69",
            "no_publicar": "<60",
        },
    }


# ============================================================
# SCANNER MANUAL
# ============================================================

class ScannerRequest(BaseModel):
    player: str
    market: str
    line: float = Field(gt=0)
    odds: float = Field(gt=1)
    probability_fa: float = Field(ge=0, le=100)
    bankroll: float = Field(default=0, ge=0)
    stake_percent: float = Field(
        default=DEFAULT_STAKE_PERCENT,
        ge=0,
        le=100,
    )
    confidence: float = Field(
        default=70,
        ge=0,
        le=100,
    )
    side: str = "over"


@app.post("/api/scanner")
def scanner(request: ScannerRequest):
    side = request.side.lower().strip()

    if side not in {"over", "under"}:
        raise HTTPException(
            status_code=400,
            detail="side debe ser 'over' o 'under'.",
        )

    edge = value_edge(
        request.probability_fa,
        request.odds,
    )

    rating = fa_rating(
        request.probability_fa,
        edge,
        request.confidence,
    )

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(request.stake_percent, MAX_STAKE_PERCENT),
    )

    return {
        "player": request.player,
        "market": request.market,
        "side": side,
        "line": request.line,
        "odds": request.odds,
        "probability_fa": request.probability_fa,
        "implied_probability": implied_probability(
            request.odds
        ),
        "value_edge": edge,
        "fa_rating": rating,
        "confidence": request.confidence,
        "risk": risk_from_rating(rating),
        "signal": signal_from_edge(edge),
        "stake_percent": stake_percent,
        "recommended_stake": calculate_stake(
            request.bankroll,
            stake_percent,
        ),
        "value_positive": edge > 0,
        "recommendation": (
            "OPORTUNIDAD CON VALOR"
            if edge > 0
            else "SIN VALOR POSITIVO"
        ),
        "motor": "Fútbol Analytics V2.2",
    }


class ProjectionScannerRequest(BaseModel):
    player: str
    market: str
    line: float = Field(gt=0)
    odds: float = Field(gt=1)
    projection: float = Field(ge=0)
    bankroll: float = Field(default=0, ge=0)
    stake_percent: float = Field(
        default=DEFAULT_STAKE_PERCENT,
        ge=0,
        le=100,
    )
    confidence: float = Field(
        default=70,
        ge=0,
        le=100,
    )
    side: str = "over"


@app.post("/api/scanner-projection")
def scanner_projection(
    request: ProjectionScannerRequest,
):
    side = request.side.lower().strip()

    if side not in {"over", "under"}:
        raise HTTPException(
            status_code=400,
            detail="side debe ser 'over' o 'under'.",
        )

    probability = (
        probability_over(
            request.line,
            request.projection,
        )
        if side == "over"
        else probability_under(
            request.line,
            request.projection,
        )
    )

    edge = value_edge(
        probability,
        request.odds,
    )

    rating = fa_rating(
        probability,
        edge,
        request.confidence,
    )

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(request.stake_percent, MAX_STAKE_PERCENT),
    )

    return {
        "player": request.player,
        "market": request.market,
        "side": side,
        "line": request.line,
        "odds": request.odds,
        "projection": request.projection,
        "probability_fa": probability,
        "implied_probability": implied_probability(
            request.odds
        ),
        "value_edge": edge,
        "fa_rating": rating,
        "confidence": request.confidence,
        "risk": risk_from_rating(rating),
        "signal": signal_from_edge(edge),
        "stake_percent": stake_percent,
        "recommended_stake": calculate_stake(
            request.bankroll,
            stake_percent,
        ),
        "value_positive": edge > 0,
        "recommendation": (
            "OPORTUNIDAD CON VALOR"
            if edge > 0
            else "SIN VALOR POSITIVO"
        ),
        "motor": "Fútbol Analytics V2.2",
    }


# ============================================================
# BANKROLL
# ============================================================

class BankrollRequest(BaseModel):
    bankroll: float = Field(ge=0)
    stake_percent: float = Field(
        default=DEFAULT_STAKE_PERCENT,
        ge=0,
        le=100,
    )


@app.post("/api/bankroll")
def bankroll_calculator(request: BankrollRequest):
    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(request.stake_percent, MAX_STAKE_PERCENT),
    )

    return {
        "bankroll": request.bankroll,
        "stake_percent": stake_percent,
        "recommended_stake": calculate_stake(
            request.bankroll,
            stake_percent,
        ),
        "minimum_stake": calculate_stake(
            request.bankroll,
            MIN_STAKE_PERCENT,
        ),
        "maximum_stake": calculate_stake(
            request.bankroll,
            MAX_STAKE_PERCENT,
        ),
        "all_in_allowed": False,
    }


# ============================================================
# BET TRACKER
# ============================================================

class BetRequest(BaseModel):
    event: str
    market: str
    odds: float = Field(gt=1)
    stake: float = Field(gt=0)
    result: str = "PENDIENTE"


@app.post("/api/bets")
def create_bet(request: BetRequest):
    result = request.result.strip().upper()
    if result not in ALLOWED_BET_RESULTS:
        raise HTTPException(
            status_code=400,
            detail="result debe ser PENDIENTE, GANADA, PERDIDA o ANULADA.",
        )

    bet = {
        "id": len(bets) + 1,
        "event": request.event,
        "market": request.market,
        "odds": request.odds,
        "stake": request.stake,
        "result": result,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    bets.append(bet)

    return {
        "status": "created",
        "bet": bet,
    }


@app.get("/api/bets")
def get_bets():
    return {
        "count": len(bets),
        "bets": bets,
    }


@app.get("/api/kpis")
def get_kpis():
    total_bets = len(bets)

    settled = [
        bet for bet in bets
        if bet["result"] in {
            "GANADA",
            "PERDIDA",
            "ANULADA",
        }
    ]

    total_staked = sum(
        safe_number(bet["stake"])
        for bet in settled
    )

    profit = 0.0
    wins = 0
    losses = 0
    voids = 0

    for bet in settled:
        stake = safe_number(bet["stake"])
        odds = safe_number(bet["odds"])
        result = bet["result"]

        if result == "GANADA":
            profit += stake * (odds - 1)
            wins += 1
        elif result == "PERDIDA":
            profit -= stake
            losses += 1
        elif result == "ANULADA":
            voids += 1

    yield_percent = (
        (profit / total_staked) * 100
        if total_staked > 0
        else 0
    )

    win_rate = (
        (wins / (wins + losses)) * 100
        if wins + losses > 0
        else 0
    )

    return {
        "total_bets": total_bets,
        "settled_bets": len(settled),
        "profit": round2(profit),
        "yield": round2(yield_percent),
        "win_rate": round2(win_rate),
        "wins": wins,
        "losses": losses,
        "voids": voids,
    }
