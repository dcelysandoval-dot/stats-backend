import os
import math
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
# FÚTBOL ANALYTICS - BACKEND V1.9
# Sportmonks principal + API-Football fallback + autenticación V3 robusta
# ============================================================

APP_VERSION = "2.0.0"
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
            if key in value:
                return safe_number(value[key])
    return safe_number(value)


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
    raw = fixture_item.get("lineups") or []
    grouped = {}

    for entry in raw:
        team_id = entry.get("team_id")
        if not team_id:
            continue

        grouped.setdefault(
            team_id,
            {
                "team": {
                    "id": team_id,
                    "name": entry.get("team_name"),
                    "logo": None,
                },
                "formation": None,
                "starters": [],
                "substitutes": [],
            },
        )

        player = {
            "player_id": entry.get("player_id"),
            "name": entry.get("player_name"),
            "number": entry.get("jersey_number"),
            "position_id": entry.get("position_id"),
            "formation_field": entry.get("formation_field"),
            "formation_position": entry.get("formation_position"),
            "starter": lineup_type_id(entry) == 11,
            "substitute": lineup_type_id(entry) == 12,
        }

        if player["starter"]:
            grouped[team_id]["starters"].append(player)
        elif player["substitute"]:
            grouped[team_id]["substitutes"].append(player)

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


async def collect_sportmonks_player_stats(
    player_id: int,
    season_id: Optional[int],
) -> dict:
    cache_key = (int(player_id), int(season_id) if season_id is not None else None)
    now = datetime.now(timezone.utc).timestamp()
    cached = PLAYER_STATS_CACHE.get(cache_key)
    if cached and now - cached["timestamp"] < PLAYER_STATS_CACHE_TTL:
        return dict(cached["stats"])

    result = await get_sportmonks_player_season(player_id, season_id)
    if not result.get("ok"):
        return {}

    item = result.get("data", {}).get("data") or {}
    stats = parse_sportmonks_statistics(item.get("statistics") or [])
    PLAYER_STATS_CACHE[cache_key] = {"timestamp": now, "stats": dict(stats)}
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
        "engine": "Player Market Scanner V1.9",
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

    for lineup in lineups:
        team = lineup.get("team") or {}
        team_id = team.get("id")

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

            data_quality = (
                "ALTA" if minutes >= 900
                else "MEDIA" if minutes >= 450
                else "BAJA"
            )

            confidence = 72 if starter else 48
            if minutes >= 900:
                confidence += 10
            elif minutes >= 450:
                confidence += 6
            elif minutes == 0:
                confidence -= 18

            confidence = max(25, min(95, confidence))

            players.append({
                "player_id": pid,
                "player": entry.get("name"),
                "team": team.get("name"),
                "team_id": team_id,
                "number": entry.get("number"),
                "position_id": entry.get("position_id"),
                "formation_field": entry.get("formation_field"),
                "starter": starter,
                "confirmed_lineup": True,
                "expected_minutes": expected_minutes,
                "stats_available": bool(stats),
                "data_quality": data_quality,
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

    fixture = normalize_sportmonks_fixture(fixture_item)

    starters = [p for p in players if p["starter"]]
    substitutes = [p for p in players if not p["starter"]]

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
        "players": players,
        "markets": SUPPORTED_MARKETS,
        "data_quality": (
            "Alineaciones y estadísticas obtenidas desde Sportmonks. "
            "Las proyecciones usan estadísticas de temporada y "
            "minutos esperados; no incluyen cuotas de bookmaker."
        ),
        "source": "Sportmonks",
        "motor": "Fútbol Analytics V1.9",
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

    if primary is not None:
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
        "motor": "Fútbol Analytics V1.9",
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

            rating = round(
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

            candidates.append({
                "player_id": player.get("player_id"),
                "player": player.get("player"),
                "team": player.get("team"),
                "market": market,
                "projection": round2(projection),
                "reference_side": best_side,
                "reference_line": round2(best_line),
                "probability_fa": round2(best_prob),
                "fa_rating": rating,
                "confidence": round2(confidence),
                "data_quality": player.get(
                    "data_quality",
                    "BAJA",
                ),
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
        "status": "scanner_ready",
        "fixture_id": fixture_id,
        "season": season,
        "count": len(candidates),
        "candidates": candidates,
        "note": (
            "Las líneas mostradas son referencias matemáticas. "
            "No representan cuotas ni líneas de bookmaker."
        ),
        "source": analysis.get("source"),
        "motor": "Fútbol Analytics V1.9",
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
        "motor": "Fútbol Analytics V1.9",
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
        "motor": "Fútbol Analytics V1.9",
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
