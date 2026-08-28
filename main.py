import os
import math
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
# FÚTBOL ANALYTICS - BACKEND V1.6
# ============================================================

APP_VERSION = "1.9.0"
CURRENT_SEASON = int(os.getenv("CURRENT_SEASON", "2026"))

app = FastAPI(
    title="Fútbol Analytics API",
    version=APP_VERSION,
    description=(
        "Player Market Scanner: selección de partido, "
        "alineaciones confirmadas, estadísticas de temporada, "
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
# API-FOOTBALL
# ============================================================

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
TEAM_PLAYER_CACHE = {}
TEAM_PLAYER_CACHE_TTL = 300


# ============================================================
# HELPERS
# ============================================================

def safe_number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def round2(value) -> float:
    return round(safe_number(value), 2)


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
    """P(X >= k) for Poisson(lambda)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0

    # Sum P(X=x) for x=0..k-1.
    term = math.exp(-lam)
    cumulative = term
    for x in range(1, k):
        term *= lam / x
        cumulative += term

    return max(0.0, min(1.0, 1.0 - cumulative))


def probability_over(line: float, projection: float) -> float:
    # Betting lines such as 0.5, 1.5, 2.5 mean X >= 1,2,3...
    target = math.floor(line) + 1
    return round(poisson_probability_at_least(target, projection) * 100, 2)


def probability_under(line: float, projection: float) -> float:
    # Under 1.5 means X <= 1. For integer lines, use X <= line-1
    # as the standard strict "under" interpretation.
    target = math.ceil(line) - 1
    if target < 0:
        return 0.0
    at_least = poisson_probability_at_least(target + 1, projection)
    return round((1.0 - at_least) * 100, 2)


# ============================================================
# API CLIENT
# ============================================================

async def apifootball_get(endpoint: str, params: Optional[dict] = None) -> dict:
    if not APIFOOTBALL_KEY:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": "APIFOOTBALL_KEY no está configurada en Render.",
        }

    headers = {"x-apisports-key": APIFOOTBALL_KEY}
    url = APIFOOTBALL_URL + endpoint

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(
                url,
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

        api_errors = data.get("errors", []) if isinstance(data, dict) else []
        if api_errors:
            return {
                "ok": False,
                "status_code": response.status_code,
                "data": data,
                "error": str(api_errors),
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
            "error": f"Error de conexión: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": str(exc),
        }


# ============================================================
# NORMALIZACIÓN DE STATS
# ============================================================

def aggregate_player_statistics(statistics: list) -> dict:
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
    }

    for stat in statistics or []:
        games = stat.get("games") or {}
        shots = stat.get("shots") or {}
        goals = stat.get("goals") or {}
        passes = stat.get("passes") or {}
        fouls = stat.get("fouls") or {}
        cards = stat.get("cards") or {}

        totals["minutes"] += safe_number(games.get("minutes"))
        totals["appearances"] += safe_number(games.get("appearences"))
        totals["starts"] += safe_number(games.get("lineups"))
        totals["shots"] += safe_number(shots.get("total"))
        totals["shots_on_target"] += safe_number(shots.get("on"))
        totals["goals"] += safe_number(goals.get("total"))
        totals["assists"] += safe_number(goals.get("assists"))
        totals["key_passes"] += safe_number(passes.get("key"))
        totals["fouls_committed"] += safe_number(fouls.get("committed"))
        totals["fouls_drawn"] += safe_number(fouls.get("drawn"))
        totals["yellow_cards"] += safe_number(cards.get("yellow"))
        totals["red_cards"] += safe_number(cards.get("red"))

    return {k: round2(v) for k, v in totals.items()}


def normalize_player_season(item: dict) -> dict:
    player = item.get("player") or {}
    stats = aggregate_player_statistics(item.get("statistics") or [])

    minutes = stats["minutes"]
    appearances = stats["appearances"]

    def per90(value: float) -> float:
        if minutes <= 0:
            return 0.0
        return round2(value / minutes * 90.0)

    return {
        "player_id": player.get("id"),
        "player": player.get("name"),
        "photo": player.get("photo"),
        "age": player.get("age"),
        **stats,
        "shots_per90": per90(stats["shots"]),
        "shots_on_target_per90": per90(stats["shots_on_target"]),
        "goals_per90": per90(stats["goals"]),
        "assists_per90": per90(stats["assists"]),
        "fouls_committed_per90": per90(stats["fouls_committed"]),
        "fouls_drawn_per90": per90(stats["fouls_drawn"]),
        "yellow_cards_per90": per90(stats["yellow_cards"]),
        "red_cards_per90": per90(stats["red_cards"]),
        "key_passes_per90": per90(stats["key_passes"]),
    }


# ============================================================
# ROOT / HEALTH
# ============================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "project": "Fútbol Analytics",
        "version": APP_VERSION,
        "engine": "Player Market Scanner V1.6",
        "api_football": bool(APIFOOTBALL_KEY),
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
        "api_football_configured": bool(APIFOOTBALL_KEY),
    }


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
# FIXTURES - PARTIDOS PARA SELECCIONAR
# ============================================================

@app.get("/api/apifootball-fixtures")
async def apifootball_fixtures(
    date: Optional[str] = None,
    league: Optional[int] = None,
    season: int = CURRENT_SEASON,
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
            "status": {
                "long": status.get("long"),
                "short": status.get("short"),
                "elapsed": status.get("elapsed"),
            },
            "league": {
                "id": league_info.get("id"),
                "name": league_info.get("name"),
                "country": league_info.get("country"),
                "season": league_info.get("season"),
            },
            "home": {
                "id": (teams.get("home") or {}).get("id"),
                "name": (teams.get("home") or {}).get("name"),
                "logo": (teams.get("home") or {}).get("logo"),
                "winner": (teams.get("home") or {}).get("winner"),
            },
            "away": {
                "id": (teams.get("away") or {}).get("id"),
                "name": (teams.get("away") or {}).get("name"),
                "logo": (teams.get("away") or {}).get("logo"),
                "winner": (teams.get("away") or {}).get("winner"),
            },
        })

    fixtures.sort(key=lambda x: x.get("timestamp") or 0)

    return {
        "status": "ok",
        "count": len(fixtures),
        "fixtures": fixtures,
        "source": "API-Football",
        "season": season,
    }


# ============================================================
# FIXTURE DETAIL
# ============================================================

@app.get("/api/fixture")
async def fixture_detail(fixture_id: int):
    result = await apifootball_get(
        "/fixtures",
        {"id": fixture_id},
    )

    if not result["ok"]:
        return {
            "status": "error",
            "fixture_id": fixture_id,
            "message": result["error"],
            "details": result["data"],
        }

    response = result["data"].get("response", [])
    if not response:
        return {
            "status": "not_found",
            "fixture_id": fixture_id,
            "message": "Fixture no encontrado.",
        }

    item = response[0]
    fixture = item.get("fixture") or {}
    teams = item.get("teams") or {}
    league_info = item.get("league") or {}

    return {
        "status": "ok",
        "fixture": {
            "fixture_id": fixture.get("id"),
            "date": fixture.get("date"),
            "timestamp": fixture.get("timestamp"),
            "status": fixture.get("status"),
            "league": league_info,
            "home": teams.get("home"),
            "away": teams.get("away"),
        },
    }


# ============================================================
# LINEUPS - ESTA ES LA CORRECCIÓN CLAVE
# ============================================================

@app.get("/api/fixture-lineups")
async def fixture_lineups(fixture_id: int):
    result = await apifootball_get(
        "/fixtures/lineups",
        {"fixture": fixture_id},
    )

    if not result["ok"]:
        return {
            "status": "error",
            "fixture_id": fixture_id,
            "available": False,
            "lineups": [],
            "message": result["error"],
            "details": result["data"],
        }

    response = result["data"].get("response", [])

    if not response:
        return {
            "status": "lineups_pending",
            "fixture_id": fixture_id,
            "available": False,
            "lineups": [],
            "message": (
                "Las alineaciones todavía no están disponibles "
                "para este fixture."
            ),
        }

    lineups = []

    for team_block in response:
        team = team_block.get("team") or {}
        coach = team_block.get("coach") or {}
        formation = team_block.get("formation")

        starters = []
        substitutes = []

        for p in team_block.get("startXI", []) or []:
            player = p.get("player") or {}
            starters.append({
                "player_id": player.get("id"),
                "name": player.get("name"),
                "number": player.get("number"),
                "position": player.get("pos"),
                "grid": player.get("grid"),
                "starter": True,
            })

        for p in team_block.get("substitutes", []) or []:
            player = p.get("player") or {}
            substitutes.append({
                "player_id": player.get("id"),
                "name": player.get("name"),
                "number": player.get("number"),
                "position": player.get("pos"),
                "starter": False,
            })

        lineups.append({
            "team": {
                "id": team.get("id"),
                "name": team.get("name"),
                "logo": team.get("logo"),
            },
            "coach": {
                "id": coach.get("id"),
                "name": coach.get("name"),
                "photo": coach.get("photo"),
            },
            "formation": formation,
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
        "source": "API-Football",
    }


# ============================================================
# PLAYER SEASON
# ============================================================

@app.get("/api/player-season")
async def player_season(
    player_id: int,
    season: int = CURRENT_SEASON,
):
    result = await apifootball_get(
        "/players",
        {"id": player_id, "season": season},
    )

    if not result["ok"]:
        return {
            "status": "error",
            "player_id": player_id,
            "statistics": [],
            "message": result["error"],
        }

    response = result["data"].get("response", [])
    if not response:
        return {
            "status": "no_data",
            "player_id": player_id,
            "statistics": [],
            "message": "No existen estadísticas para este jugador.",
        }

    normalized = normalize_player_season(response[0])

    return {
        "status": "ok",
        "season": season,
        "player": normalized,
        "source": "API-Football",
    }


# ============================================================
# FIXTURE ANALYSIS
# ============================================================

async def fetch_team_players_all_pages(team_id: int, season: int) -> list:
    """
    Recupera las páginas de /players necesarias para encontrar a los
    titulares y suplentes de una alineación confirmada.

    Se usa una caché corta para no repetir llamadas al seleccionar
    nuevamente el mismo partido.
    """
    import time

    cache_key = (int(team_id), int(season))
    now = time.time()
    cached = TEAM_PLAYER_CACHE.get(cache_key)
    if cached and now - cached["time"] < TEAM_PLAYER_CACHE_TTL:
        return cached["players"]

    all_items = []
    page = 1
    max_pages = 5

    while page <= max_pages:
        result = await apifootball_get(
            "/players",
            {
                "team": team_id,
                "season": season,
                "page": page,
            },
        )
        if not result["ok"]:
            break

        data = result["data"] or {}
        items = data.get("response", []) or []
        all_items.extend(items)

        paging = data.get("paging") or {}
        current = safe_number(paging.get("current")) or page
        total = safe_number(paging.get("total")) or current

        if current >= total or not items:
            break
        page += 1

    TEAM_PLAYER_CACHE[cache_key] = {
        "time": now,
        "players": all_items,
    }
    return all_items


@app.get("/api/fixture-analysis")
async def fixture_analysis(
    fixture_id: int,
    season: int = CURRENT_SEASON,
):
    """
    Flujo pre-partido:
    1) fixture
    2) lineups
    3) estadísticas de temporada de cada equipo
    4) merge por player_id
    5) proyecciones por 90 y minutos esperados
    """

    fixture_result = await apifootball_get(
        "/fixtures",
        {"id": fixture_id},
    )

    if not fixture_result["ok"]:
        return {
            "status": "error",
            "fixture_id": fixture_id,
            "message": fixture_result["error"],
            "details": fixture_result["data"],
        }

    fixtures = fixture_result["data"].get("response", [])
    if not fixtures:
        return {
            "status": "not_found",
            "fixture_id": fixture_id,
            "message": "Fixture no encontrado.",
        }

    fixture_item = fixtures[0]
    fixture = fixture_item.get("fixture") or {}
    teams = fixture_item.get("teams") or {}
    league = fixture_item.get("league") or {}

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
            "message": (
                "No fue posible obtener las alineaciones todavía."
            ),
            "details": lineup_result["data"],
        }

    lineup_response = lineup_result["data"].get("response", [])
    if not lineup_response:
        return {
            "status": "lineups_pending",
            "available": False,
            "fixture_id": fixture_id,
            "players": [],
            "message": (
                "Las alineaciones todavía no están disponibles. "
                "Vuelve a consultar cuando estén confirmadas."
            ),
        }

    # Obtener estadísticas de temporada por equipo: 2 llamadas,
    # en lugar de una llamada por jugador.
    season_stats_by_player = {}

    team_ids = []
    for team_block in lineup_response:
        tid = (team_block.get("team") or {}).get("id")
        if tid:
            team_ids.append(tid)

    for team_id in dict.fromkeys(team_ids):
        team_player_items = await fetch_team_players_all_pages(
            team_id,
            season,
        )

        for item in team_player_items:
            normalized = normalize_player_season(item)
            pid = normalized.get("player_id")
            if pid:
                season_stats_by_player[pid] = normalized

    players = []

    for team_block in lineup_response:
        team = team_block.get("team") or {}
        team_id = team.get("id")
        team_name = team.get("name")

        starter_ids = {
            (x.get("player") or {}).get("id")
            for x in team_block.get("startXI", []) or []
        }

        all_lineup_players = (
            list(team_block.get("startXI", []) or [])
            + list(team_block.get("substitutes", []) or [])
        )

        for entry in all_lineup_players:
            p = entry.get("player") or {}
            pid = p.get("id")
            if not pid:
                continue

            season_stats = season_stats_by_player.get(pid, {})

            minutes = safe_number(season_stats.get("minutes"))
            starts = safe_number(season_stats.get("starts"))
            appearances = safe_number(season_stats.get("appearances"))

            # Si es titular confirmado, usamos una expectativa prudente.
            # No asumimos 90 minutos automáticamente.
            expected_minutes = 75.0 if pid in starter_ids else 20.0

            # Si tiene mucha presencia como titular, elevamos ligeramente
            # la expectativa sin pasar de 85.
            if pid in starter_ids and appearances > 0:
                start_rate = starts / appearances
                if start_rate >= 0.75:
                    expected_minutes = 80.0
                elif start_rate >= 0.50:
                    expected_minutes = 77.0

            def project(per90_key: str) -> float:
                per90 = safe_number(season_stats.get(per90_key))
                return round2(per90 * expected_minutes / 90.0)

            stats_available = bool(season_stats)
            data_quality = "ALTA" if minutes >= 450 else ("MEDIA" if minutes > 0 else "BAJA")
            base_confidence = 72 if pid in starter_ids else 48
            if minutes >= 900:
                base_confidence += 10
            elif minutes >= 450:
                base_confidence += 6
            elif minutes == 0:
                base_confidence -= 18
            base_confidence = max(25, min(95, base_confidence))

            player_row = {
                "player_id": pid,
                "player": p.get("name"),
                "team": team_name,
                "team_id": team_id,
                "number": p.get("number"),
                "position": p.get("pos"),
                "starter": pid in starter_ids,
                "confirmed_lineup": True,
                "expected_minutes": expected_minutes,
                "stats_available": stats_available,
                "data_quality": data_quality,
                "confidence": base_confidence,

                "minutes_season": minutes,
                "appearances_season": appearances,
                "starts_season": starts,

                "shots_season": season_stats.get("shots", 0),
                "shots_on_target_season": season_stats.get("shots_on_target", 0),
                "goals_season": season_stats.get("goals", 0),
                "assists_season": season_stats.get("assists", 0),
                "fouls_committed_season": season_stats.get("fouls_committed", 0),
                "fouls_drawn_season": season_stats.get("fouls_drawn", 0),
                "yellow_cards_season": season_stats.get("yellow_cards", 0),

                "shots_per90": season_stats.get("shots_per90", 0),
                "shots_on_target_per90": season_stats.get("shots_on_target_per90", 0),
                "goals_per90": season_stats.get("goals_per90", 0),
                "assists_per90": season_stats.get("assists_per90", 0),
                "fouls_committed_per90": season_stats.get("fouls_committed_per90", 0),
                "fouls_drawn_per90": season_stats.get("fouls_drawn_per90", 0),
                "yellow_cards_per90": season_stats.get("yellow_cards_per90", 0),

                "projection": {
                    "Remates": project("shots_per90"),
                    "Remates a puerta": project("shots_on_target_per90"),
                    "Goles": project("goals_per90"),
                    "Asistencias": project("assists_per90"),
                    "Faltas": project("fouls_committed_per90"),
                    "Tarjetas": project("yellow_cards_per90"),
                },
            }

            players.append(player_row)

    starters = [p for p in players if p["starter"]]
    substitutes = [p for p in players if not p["starter"]]

    return {
        "status": "real_player_data",
        "available": True,
        "fixture_id": fixture_id,
        "season": season,
        "fixture": {
            "date": fixture.get("date"),
            "status": fixture.get("status"),
            "league": league,
            "home": teams.get("home"),
            "away": teams.get("away"),
        },
        "lineups_confirmed": True,
        "count": len(players),
        "starter_count": len(starters),
        "substitute_count": len(substitutes),
        "players": players,
        "markets": SUPPORTED_MARKETS,
        "data_quality": (
            "Las alineaciones son confirmadas. "
            "Las proyecciones se basan en estadísticas de temporada "
            "y minutos esperados; no incluyen cuotas de bookmaker."
        ),
        "source": "API-Football",
        "motor": "Fútbol Analytics V1.6",
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


def model_probability_for_side(line: float, projection: float, side: str) -> float:
    if side == "over":
        return probability_over(line, projection)
    return probability_under(line, projection)


def build_market_candidates(players: list, limit: int = 6) -> list:
    candidates = []

    for player in players or []:
        if not player.get("starter"):
            continue

        for market, key in MARKET_KEYS.items():
            projection = safe_number(player.get("projection", {}).get(market))
            if projection <= 0.05:
                continue

            over_line = max(0.5, math.floor(projection) + 0.5)
            under_line = max(0.5, math.ceil(projection) + 0.5)

            over_prob = model_probability_for_side(
                over_line, projection, "over"
            )
            under_prob = model_probability_for_side(
                under_line, projection, "under"
            )

            confidence = safe_number(player.get("confidence")) or 50
            best_side = "over" if over_prob >= under_prob else "under"
            best_line = over_line if best_side == "over" else under_line
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
                "data_quality": player.get("data_quality", "BAJA"),
                "confirmed_lineup": bool(player.get("confirmed_lineup")),
            })

    candidates.sort(
        key=lambda x: (
            x["fa_rating"],
            x["probability_fa"],
            x["projection"],
        ),
        reverse=True,
    )
    return candidates[: max(1, min(limit, 20))]


@app.get("/api/player-market-scan")
async def player_market_scan(
    fixture_id: int,
    season: int = CURRENT_SEASON,
    limit: int = 6,
):
    """
    Devuelve candidatos estadísticos pre-partido.
    Las líneas son referencias matemáticas del modelo, NO líneas
    de bookmaker. El Value Edge solo se calcula cuando existe una
    cuota real.
    """
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
        "source": "API-Football",
        "motor": "Fútbol Analytics V1.9",
    }


# ============================================================
# SCANNER
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
    confidence: float = Field(default=70, ge=0, le=100)
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

    risk = risk_from_rating(rating)
    signal = signal_from_edge(edge)
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
        "implied_probability": implied_probability(request.odds),
        "value_edge": edge,
        "fa_rating": rating,
        "confidence": request.confidence,
        "risk": risk,
        "signal": signal,
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
        "motor": "Fútbol Analytics V1.6",
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
    confidence: float = Field(default=70, ge=0, le=100)
    side: str = "over"


@app.post("/api/scanner-projection")
def scanner_projection(request: ProjectionScannerRequest):
    side = request.side.lower().strip()
    if side not in {"over", "under"}:
        raise HTTPException(
            status_code=400,
            detail="side debe ser 'over' o 'under'.",
        )

    if side == "over":
        probability = probability_over(
            request.line,
            request.projection,
        )
    else:
        probability = probability_under(
            request.line,
            request.projection,
        )

    edge = value_edge(probability, request.odds)
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
        "implied_probability": implied_probability(request.odds),
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
        "motor": "Fútbol Analytics V1.6",
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
    bet = {
        "id": len(bets) + 1,
        "event": request.event,
        "market": request.market,
        "odds": request.odds,
        "stake": request.stake,
        "result": request.result,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
        if bet["result"] in {"GANADA", "PERDIDA", "ANULADA"}
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
