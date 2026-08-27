import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# FÚTBOL ANALYTICS - BACKEND V1.5
# ============================================================

app = FastAPI(
    title="Fútbol Analytics API",
    version="1.5.0",
    description=(
        "Motor de análisis estadístico, Player Market Scanner, "
        "API-Football, Value Edge y gestión de bankroll."
    )
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# CONFIGURACIÓN FOOTBALL-DATA.ORG
# ============================================================

FOOTBALL_DATA_API_KEY = os.getenv(
    "FOOTBALL_DATA_API_KEY",
    ""
)

BASE_URL = "https://api.football-data.org/v4"

FREE_LEAGUES = "PL,PD,SA,BL1,FL1,CL,EC,WC"


# ============================================================
# CONFIGURACIÓN API-FOOTBALL / API-SPORTS
# ============================================================

APIFOOTBALL_KEY = os.getenv(
    "APIFOOTBALL_KEY",
    ""
)

APIFOOTBALL_URL = "https://v3.football.api-sports.io"


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

CURRENT_SEASON = 2026

SUPPORTED_MARKETS = [
    "Remates",
    "Remates a puerta",
    "Goles",
    "Asistencias",
    "Faltas",
    "Tarjetas"
]


# ============================================================
# CONFIGURACIÓN BANKROLL
# ============================================================

MIN_STAKE_PERCENT = 1.0
DEFAULT_STAKE_PERCENT = 2.0
MAX_STAKE_PERCENT = 3.0


# ============================================================
# MEMORIA TEMPORAL DE APUESTAS
# ============================================================

bets = []


# ============================================================
# FUNCIONES DEL MOTOR ANALÍTICO
# ============================================================

def calcular_indice_ivj(match: dict) -> float:

    score = match.get(
        "score",
        {}
    ).get(
        "fullTime",
        {}
    )

    home_score = score.get(
        "home",
        0
    ) or 0

    away_score = score.get(
        "away",
        0
    ) or 0

    ivj_base = (
        5.0
        + (home_score + away_score) * 0.5
    )

    return round(
        ivj_base,
        2
    )


def probabilidad_implicita(
    cuota: float
) -> float:

    if cuota <= 1:
        return 0.0

    return round(
        (1 / cuota) * 100,
        2
    )


def calcular_value_edge(
    probabilidad_fa: float,
    cuota: float
) -> float:

    probabilidad_cuota = (
        probabilidad_implicita(cuota)
    )

    return round(
        probabilidad_fa - probabilidad_cuota,
        2
    )


def calcular_fa_rating(
    probabilidad_fa: float,
    value_edge: float,
    confianza: float = 70
) -> int:

    score = (
        probabilidad_fa * 0.45
        + max(value_edge, 0) * 1.5
        + confianza * 0.25
    )

    score = max(
        0,
        min(score, 100)
    )

    return round(score)


def calcular_stake(
    bankroll: float,
    stake_percent: float
) -> float:

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            stake_percent,
            MAX_STAKE_PERCENT
        )
    )

    return round(
        bankroll * (
            stake_percent / 100
        ),
        2
    )


def determinar_riesgo(
    fa_rating: int
) -> str:

    if fa_rating >= 80:
        return "BAJO"

    if fa_rating >= 70:
        return "MEDIO"

    if fa_rating >= 60:
        return "MODERADO"

    return "ALTO"


def determinar_senal(
    value_edge: float
) -> str:

    if value_edge >= 15:
        return "OPORTUNIDAD ALTA"

    if value_edge >= 8:
        return "OPORTUNIDAD MEDIA"

    if value_edge > 0:
        return "VALOR BAJO"

    return "SIN VALOR"


# ============================================================
# FUNCIONES API-FOOTBALL
# ============================================================

async def apifootball_get(
    endpoint: str,
    params: Optional[dict] = None
):

    if not APIFOOTBALL_KEY:

        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": (
                "APIFOOTBALL_KEY no está "
                "configurada en Render."
            )
        }

    headers = {
        "x-apisports-key": APIFOOTBALL_KEY
    }

    url = (
        APIFOOTBALL_URL
        + endpoint
    )

    try:

        async with httpx.AsyncClient(
            timeout=20.0
        ) as client:

            response = await client.get(
                url,
                headers=headers,
                params=params or {}
            )

            try:
                data = response.json()
            except Exception:
                data = {
                    "raw": response.text
                }

            if response.status_code != 200:

                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "data": data,
                    "error": (
                        "API-Football respondió "
                        f"con HTTP {response.status_code}"
                    )
                }

            api_errors = (
                data.get("errors", [])
                if isinstance(data, dict)
                else []
            )

            if api_errors:

                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "data": data,
                    "error": str(api_errors)
                }

            return {
                "ok": True,
                "status_code": response.status_code,
                "data": data,
                "error": None
            }

    except httpx.RequestError as exc:

        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": str(exc)
        }

    except Exception as exc:

        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": str(exc)
        }


# ============================================================
# NORMALIZADORES
# ============================================================

def safe_number(value):

    if value is None:
        return 0

    if isinstance(
        value,
        (int, float)
    ):
        return value

    try:
        return float(value)

    except Exception:
        return 0


def normalizar_player_statistics(
    player_block: dict
) -> dict:

    player = player_block.get(
        "player",
        {}
    )

    statistics = player_block.get(
        "statistics",
        []
    )

    if not statistics:
        statistics = [{}]

    stats = statistics[0] or {}

    shots = stats.get(
        "shots",
        {}
    ) or {}

    goals = stats.get(
        "goals",
        {}
    ) or {}

    passes = stats.get(
        "passes",
        {}
    ) or {}

    fouls = stats.get(
        "fouls",
        {}
    ) or {}

    cards = stats.get(
        "cards",
        {}
    ) or {}

    games = stats.get(
        "games",
        {}
    ) or {}

    return {

        "player_id": player.get(
            "id"
        ),

        "player": player.get(
            "name"
        ),

        "photo": player.get(
            "photo"
        ),

        "team": (
            player_block.get(
                "team",
                {}
            ).get(
                "name"
            )
        ),

        "team_id": (
            player_block.get(
                "team",
                {}
            ).get(
                "id"
            )
        ),

        "position": games.get(
            "position"
        ),

        "minutes": safe_number(
            games.get(
                "minutes"
            )
        ),

        "rating": safe_number(
            games.get(
                "rating"
            )
        ),

        "starter": games.get(
            "substitute"
        ) is False,

        "shots": safe_number(
            shots.get(
                "total"
            )
        ),

        "shots_on_target": safe_number(
            shots.get(
                "on"
            )
        ),

        "goals": safe_number(
            goals.get(
                "total"
            )
        ),

        "assists": safe_number(
            goals.get(
                "assists"
            )
        ),

        "key_passes": safe_number(
            passes.get(
                "key"
            )
        ),

        "fouls_committed": safe_number(
            fouls.get(
                "committed"
            )
        ),

        "fouls_drawn": safe_number(
            fouls.get(
                "drawn"
            )
        ),

        "yellow_cards": safe_number(
            cards.get(
                "yellow"
            )
        ),

        "red_cards": safe_number(
            cards.get(
                "red"
            )
        )
    }


# ============================================================
# NORMALIZADOR DE ALINEACIONES
# ============================================================

def normalizar_lineup_player(
    player_block: dict,
    team_info: dict,
    starter: bool = False
) -> dict:

    player = player_block.get(
        "player",
        {}
    )

    return {

        "player_id":
            player.get("id"),

        "player":
            player.get("name"),

        "photo":
            player.get("photo"),

        "team":
            team_info.get("name"),

        "team_id":
            team_info.get("id"),

        "number":
            player.get("number"),

        "position":
            player.get("pos"),

        "grid":
            player.get("grid"),

        "starter":
            starter,

        "confirmed_lineup":
            True
    }


# ============================================================
# OBTENER ALINEACIONES
# ============================================================

async def obtener_lineups(
    fixture_id: int
):

    result = await apifootball_get(
        "/fixtures/lineups",
        {
            "fixture": fixture_id
        }
    )

    if not result["ok"]:

        return {
            "ok": False,
            "lineups": [],
            "error": result["error"],
            "data": result["data"]
        }

    lineups = []

    for team_block in result[
        "data"
    ].get(
        "response",
        []
    ):

        team_info = team_block.get(
            "team",
            {}
        )

        # ----------------------------------------------------
        # TITULARES
        # ----------------------------------------------------

        for player_block in team_block.get(
            "startXI",
            []
        ):

            player = normalizar_lineup_player(
                player_block,
                team_info,
                starter=True
            )

            lineups.append(
                player
            )

        # ----------------------------------------------------
        # SUPLENTES
        # ----------------------------------------------------

        for player_block in team_block.get(
            "substitutes",
            []
        ):

            player = normalizar_lineup_player(
                player_block,
                team_info,
                starter=False
            )

            lineups.append(
                player
            )

    return {
        "ok": True,
        "lineups": lineups,
        "error": None,
        "data": result["data"]
    }


# ============================================================
# ESTADÍSTICAS DE EQUIPO
# ============================================================

async def obtener_estadisticas_equipo(
    team_id: int,
    season: int = CURRENT_SEASON
):

    if not team_id:

        return {
            "ok": False,
            "players": [],
            "error": "team_id no válido"
        }

    result = await apifootball_get(
        "/players",
        {
            "team": team_id,
            "season": season,
            "page": 1
        }
    )

    if not result["ok"]:

        return {
            "ok": False,
            "players": [],
            "error": result["error"]
        }

    players = []

    data = result["data"]

    for item in data.get(
        "response",
        []
    ):

        normalized = normalizar_player_statistics(
            item
        )

        players.append(
            normalized
        )

    return {
        "ok": True,
        "players": players,
        "error": None
    }


# ============================================================
# OBTENER ESTADÍSTICAS DIRECTAS DE UN JUGADOR
# ============================================================

async def obtener_estadisticas_jugador(
    player_id: int,
    season: int = CURRENT_SEASON
):

    if not player_id:

        return None

    result = await apifootball_get(
        "/players",
        {
            "id": player_id,
            "season": season
        }
    )

    if not result["ok"]:

        return None

    response = result[
        "data"
    ].get(
        "response",
        []
    )

    if not response:

        return None

    normalized = normalizar_player_statistics(
        response[0]
    )

    return normalized


# ============================================================
# MODELOS
# ============================================================

class BankrollRequest(BaseModel):

    bankroll: float

    stake_percent: float = (
        DEFAULT_STAKE_PERCENT
    )


class ScannerRequest(BaseModel):

    player: str

    market: str

    line: float

    odds: float

    probability_fa: float

    bankroll: float = 0

    stake_percent: float = (
        DEFAULT_STAKE_PERCENT
    )

    confidence: float = 70


class BetRequest(BaseModel):

    event: str

    market: str

    odds: float

    stake: float

    result: str = "PENDIENTE"


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def read_root():

    return {

        "status": "ok",

        "project": "Fútbol Analytics",

        "version": "1.5.0",

        "engine":
            "Player Market Scanner V1.5",

        "api_football":
            bool(APIFOOTBALL_KEY),

        "message":
            "Fútbol Analytics API corriendo"
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return {

        "status": "healthy",

        "service":
            "futbol-analytics-backend",

        "version":
            "1.5.0",

        "api_football_configured":
            bool(APIFOOTBALL_KEY)
    }


# ============================================================
# TEST API-FOOTBALL
# ============================================================

@app.get("/api/test-apifootball")
async def test_apifootball():

    result = await apifootball_get(
        "/status"
    )

    if not result["ok"]:

        return {

            "status": "error",

            "api_configured":
                bool(APIFOOTBALL_KEY),

            "status_code":
                result["status_code"],

            "message":
                result["error"]
        }

    return {

        "status":
            "connected",

        "api_configured":
            True,

        "status_code":
            result["status_code"],

        "api_response":
            result["data"]
    }


# ============================================================
# PARTIDOS FOOTBALL-DATA.ORG
# ============================================================

@app.get("/api/matches")
async def get_matches(
    days_ahead: int = 1
):

    if not FOOTBALL_DATA_API_KEY:

        return {

            "count": 0,

            "matches": [],

            "error": (
                "FOOTBALL_DATA_API_KEY "
                "no está configurada."
            )
        }

    headers = {
        "X-Auth-Token":
            FOOTBALL_DATA_API_KEY
    }

    now_utc = datetime.now(
        timezone.utc
    )

    date_from = now_utc.strftime(
        "%Y-%m-%d"
    )

    date_to = (
        now_utc
        + timedelta(
            days=days_ahead
        )
    ).strftime(
        "%Y-%m-%d"
    )

    url = (
        f"{BASE_URL}/matches"
        f"?dateFrom={date_from}"
        f"&dateTo={date_to}"
        f"&competitions={FREE_LEAGUES}"
    )

    try:

        async with httpx.AsyncClient(
            timeout=15.0
        ) as client:

            response = await client.get(
                url,
                headers=headers
            )

            if response.status_code != 200:

                try:
                    details = response.json()

                except Exception:
                    details = response.text

                return {

                    "count": 0,

                    "matches": [],

                    "error": (
                        "Error en API externa "
                        f"({response.status_code})"
                    ),

                    "details":
                        details
                }

            data = response.json()

            raw_matches = data.get(
                "matches",
                []
            )

            processed_matches = []

            for match in raw_matches:

                competition = match.get(
                    "competition",
                    {}
                )

                home_team = match.get(
                    "homeTeam",
                    {}
                )

                away_team = match.get(
                    "awayTeam",
                    {}
                )

                full_time = (
                    match.get(
                        "score",
                        {}
                    ).get(
                        "fullTime",
                        {}
                    )
                )

                processed_matches.append({

                    "id":
                        match.get("id"),

                    "utcDate":
                        match.get("utcDate"),

                    "status":
                        match.get("status"),

                    "competition": {

                        "name":
                            competition.get("name"),

                        "emblem":
                            competition.get("emblem")
                    },

                    "homeTeam": {

                        "name":
                            home_team.get("name"),

                        "crest":
                            home_team.get("crest")
                    },

                    "awayTeam": {

                        "name":
                            away_team.get("name"),

                        "crest":
                            away_team.get("crest")
                    },

                    "score":
                        full_time,

                    "ivjIndex":
                        calcular_indice_ivj(
                            match
                        )
                })

            return {

                "count":
                    len(processed_matches),

                "dateFrom":
                    date_from,

                "dateTo":
                    date_to,

                "matches":
                    processed_matches
            }

    except Exception as exc:

        return {

            "count": 0,

            "matches": [],

            "error":
                "Error procesando partidos",

            "details":
                str(exc)
        }


# ============================================================
# COMPATIBILIDAD FRONTEND
# ============================================================

@app.get("/api/partidos-hoy")
async def partidos_hoy():

    return await get_matches(
        days_ahead=1
    )


# ============================================================
# API-FOOTBALL - FIXTURES
# ============================================================

@app.get("/api/apifootball-fixtures")
async def apifootball_fixtures(
    date: Optional[str] = None,
    league: Optional[int] = None,
    season: int = CURRENT_SEASON,
    next: int = 10
):

    params = {}

    if date:

        params["date"] = date

    elif league:

        params["league"] = league

        params["season"] = season

        params["next"] = next

    else:

        params["date"] = (
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d"
            )
        )

    result = await apifootball_get(
        "/fixtures",
        params
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "count":
                0,

            "fixtures":
                [],

            "message":
                result["error"],

            "details":
                result["data"]
        }

    data = result["data"]

    fixtures = []

    for fixture in data.get(
        "response",
        []
    ):

        fixture_info = fixture.get(
            "fixture",
            {}
        )

        teams = fixture.get(
            "teams",
            {}
        )

        league_info = fixture.get(
            "league",
            {}
        )

        fixtures.append({

            "fixture_id":
                fixture_info.get("id"),

            "date":
                fixture_info.get("date"),

            "status":
                fixture_info.get(
                    "status",
                    {}
                ),

            "league": {

                "id":
                    league_info.get("id"),

                "name":
                    league_info.get("name"),

                "country":
                    league_info.get("country")
            },

            "home": {

                "id":
                    teams.get(
                        "home",
                        {}
                    ).get("id"),

                "name":
                    teams.get(
                        "home",
                        {}
                    ).get("name"),

                "logo":
                    teams.get(
                        "home",
                        {}
                    ).get("logo")
            },

            "away": {

                "id":
                    teams.get(
                        "away",
                        {}
                    ).get("id"),

                "name":
                    teams.get(
                        "away",
                        {}
                    ).get("name"),

                "logo":
                    teams.get(
                        "away",
                        {}
                    ).get("logo")
            }
        })

    return {

        "status":
            "ok",

        "count":
            len(fixtures),

        "fixtures":
            fixtures,

        "source":
            "API-Football",

        "season":
            season
    }


# ============================================================
# API-FOOTBALL - JUGADORES DE FIXTURE
# ============================================================

@app.get("/api/fixture-players")
async def fixture_players(
    fixture_id: int
):

    result = await apifootball_get(
        "/fixtures/players",
        {
            "fixture": fixture_id
        }
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "fixture_id":
                fixture_id,

            "players":
                [],

            "message":
                result["error"],

            "details":
                result["data"]
        }

    data = result["data"]

    players = []

    for team_block in data.get(
        "response",
        []
    ):

        team_info = team_block.get(
            "team",
            {}
        )

        for player_block in (
            team_block.get(
                "players",
                []
            )
        ):

            normalized = (
                normalizar_player_statistics(
                    player_block
                )
            )

            normalized["team"] = (
                team_info.get("name")
            )

            normalized["team_id"] = (
                team_info.get("id")
            )

            players.append(
                normalized
            )

    return {

        "status":
            "ok",

        "fixture_id":
            fixture_id,

        "count":
            len(players),

        "players":
            players,

        "source":
            "API-Football"
    }


# ============================================================
# API-FOOTBALL - ALINEACIONES
# ============================================================

@app.get("/api/fixture-lineups")
async def fixture_lineups(
    fixture_id: int
):

    result = await obtener_lineups(
        fixture_id
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "available":
                False,

            "fixture_id":
                fixture_id,

            "players":
                [],

            "message":
                result["error"]
        }

    starters = [
        player
        for player in result["lineups"]
        if player.get("starter")
    ]

    substitutes = [
        player
        for player in result["lineups"]
        if not player.get("starter")
    ]

    return {

        "status":
            "ok",

        "available":
            True,

        "fixture_id":
            fixture_id,

        "count":
            len(result["lineups"]),

        "starters_count":
            len(starters),

        "substitutes_count":
            len(substitutes),

        "starters":
            starters,

        "substitutes":
            substitutes,

        "players":
            result["lineups"],

        "source":
            "API-Football"
    }


# ============================================================
# API-FOOTBALL - BUSCAR JUGADOR
# ============================================================

@app.get("/api/player-search")
async def player_search(
    search: str,
    season: int = CURRENT_SEASON
):

    result = await apifootball_get(
        "/players",
        {
            "search": search,
            "season": season
        }
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "players":
                [],

            "message":
                result["error"]
        }

    players = []

    for item in result[
        "data"
    ].get(
        "response",
        []
    ):

        player = item.get(
            "player",
            {}
        )

        players.append({

            "id":
                player.get("id"),

            "name":
                player.get("name"),

            "firstname":
                player.get("firstname"),

            "lastname":
                player.get("lastname"),

            "age":
                player.get("age"),

            "nationality":
                player.get("nationality"),

            "photo":
                player.get("photo")
        })

    return {

        "status":
            "ok",

        "count":
            len(players),

        "players":
            players,

        "source":
            "API-Football"
    }


# ============================================================
# API-FOOTBALL - ESTADÍSTICAS DE TEMPORADA
# ============================================================

@app.get("/api/player-season")
async def player_season(
    player_id: int,
    season: int = CURRENT_SEASON
):

    result = await apifootball_get(
        "/players",
        {
            "id":
                player_id,

            "season":
                season
        }
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "player_id":
                player_id,

            "statistics":
                [],

            "message":
                result["error"]
        }

    response = result[
        "data"
    ].get(
        "response",
        []
    )

    if not response:

        return {

            "status":
                "no_data",

            "player_id":
                player_id,

            "statistics":
                [],

            "message":
                "No existen estadísticas para este jugador."
        }

    player = response[0].get(
        "player",
        {}
    )

    statistics = []

    for item in response[0].get(
        "statistics",
        []
    ):

        statistics.append({

            "team":
                item.get(
                    "team",
                    {}
                ),

            "league":
                item.get(
                    "league",
                    {}
                ),

            "games":
                item.get(
                    "games",
                    {}
                ),

            "shots":
                item.get(
                    "shots",
                    {}
                ),

            "goals":
                item.get(
                    "goals",
                    {}
                ),

            "passes":
                item.get(
                    "passes",
                    {}
                ),

            "fouls":
                item.get(
                    "fouls",
                    {}
                ),

            "cards":
                item.get(
                    "cards",
                    {}
                )
        })

    return {

        "status":
            "ok",

        "player": {

            "id":
                player.get("id"),

            "name":
                player.get("name"),

            "photo":
                player.get("photo")
        },

        "season":
            season,

        "statistics":
            statistics,

        "source":
            "API-Football"
    }


# ============================================================
# PLAYER MARKET SCANNER - MOTOR
# ============================================================

@app.post("/api/scanner")
def scanner(
    request: ScannerRequest
):

    implied_probability = (
        probabilidad_implicita(
            request.odds
        )
    )

    value_edge = (
        calcular_value_edge(
            request.probability_fa,
            request.odds
        )
    )

    fa_rating = (
        calcular_fa_rating(
            request.probability_fa,
            value_edge,
            request.confidence
        )
    )

    risk = determinar_riesgo(
        fa_rating
    )

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            request.stake_percent,
            MAX_STAKE_PERCENT
        )
    )

    stake = calcular_stake(
        request.bankroll,
        stake_percent
    )

    signal = determinar_senal(
        value_edge
    )

    value_positive = (
        value_edge > 0
    )

    return {

        "player":
            request.player,

        "market":
            request.market,

        "line":
            request.line,

        "odds":
            request.odds,

        "probability_fa":
            request.probability_fa,

        "implied_probability":
            implied_probability,

        "value_edge":
            value_edge,

        "fa_rating":
            fa_rating,

        "confidence":
            request.confidence,

        "risk":
            risk,

        "signal":
            signal,

        "stake_percent":
            stake_percent,

        "recommended_stake":
            stake,

        "value_positive":
            value_positive,

        "recommendation": (
            "OPORTUNIDAD CON VALOR"
            if value_positive
            else "SIN VALOR POSITIVO"
        ),

        "motor":
            "Fútbol Analytics V1.5"
    }


# ============================================================
# PLAYER MARKET - PRE-PARTIDO
# ============================================================

@app.get("/api/player-market")
async def player_market(
    fixture_id: Optional[int] = None
):

    # --------------------------------------------------------
    # 1. COMPROBAR API
    # --------------------------------------------------------

    if not APIFOOTBALL_KEY:

        return {

            "status":
                "waiting_api",

            "available":
                False,

            "senal":
                "SIN DATOS",

            "signal":
                "SIN DATOS",

            "motor":
                "Fútbol Analytics V1.5",

            "message": (
                "Configura APIFOOTBALL_KEY "
                "en Render."
            )
        }

    # --------------------------------------------------------
    # 2. BUSCAR FIXTURE
    # --------------------------------------------------------

    if fixture_id is None:

        fixture_result = (
            await apifootball_get(
                "/fixtures",
                {
                    "date":
                        datetime.now(
                            timezone.utc
                        ).strftime(
                            "%Y-%m-%d"
                        )
                }
            )
        )

        if not fixture_result["ok"]:

            return {

                "status":
                    "error",

                "available":
                    False,

                "message":
                    fixture_result["error"]
            }

        fixtures = (
            fixture_result[
                "data"
            ].get(
                "response",
                []
            )
        )

        upcoming = []

        for fixture in fixtures:

            status = fixture.get(
                "fixture",
                {}
            ).get(
                "status",
                {}
            ).get(
                "short"
            )

            if status in [
                "NS",
                "TBD"
            ]:

                upcoming.append(
                    fixture
                )

        if not upcoming:

            return {

                "status":
                    "waiting_fixture",

                "available":
                    False,

                "senal":
                    "SIN DATOS",

                "signal":
                    "SIN DATOS",

                "motor":
                    "Fútbol Analytics V1.5",

                "message": (
                    "No hay un fixture "
                    "próximo disponible."
                )
            }

        fixture = upcoming[0]

        fixture_id = (
            fixture.get(
                "fixture",
                {}
            ).get(
                "id"
            )
        )

    # --------------------------------------------------------
    # 3. OBTENER FIXTURE
    # --------------------------------------------------------

    fixture_result = await apifootball_get(
        "/fixtures",
        {
            "id":
                fixture_id
        }
    )

    if not fixture_result["ok"]:

        return {

            "status":
                "fixture_error",

            "available":
                False,

            "fixture_id":
                fixture_id,

            "message":
                fixture_result["error"]
        }

    fixture_response = (
        fixture_result["data"]
        .get(
            "response",
            []
        )
    )

    if not fixture_response:

        return {

            "status":
                "fixture_not_found",

            "available":
                False,

            "fixture_id":
                fixture_id,

            "message":
                "No se encontró el fixture."
        }

    fixture_data = fixture_response[0]

    teams = fixture_data.get(
        "teams",
        {}
    )

    home_team = teams.get(
        "home",
        {}
    )

    away_team = teams.get(
        "away",
        {}
    )

    # --------------------------------------------------------
    # 4. OBTENER ALINEACIONES
    # --------------------------------------------------------

    lineup_result = await obtener_lineups(
        fixture_id
    )

    if not lineup_result["ok"]:

        return {

            "status":
                "lineups_pending",

            "available":
                False,

            "fixture_id":
                fixture_id,

            "players":
                [],

            "senal":
                "SIN DATOS",

            "signal":
                "SIN DATOS",

            "motor":
                "Fútbol Analytics V1.5",

            "message": (
                "Las alineaciones todavía "
                "no están disponibles."
            ),

            "details":
                lineup_result["error"]
        }

    lineups = lineup_result[
        "lineups"
    ]

    if not lineups:

        return {

            "status":
                "lineups_pending",

            "available":
                False,

            "fixture_id":
                fixture_id,

            "players":
                [],

            "senal":
                "SIN DATOS",

            "signal":
                "SIN DATOS",

            "motor":
                "Fútbol Analytics V1.5",

            "message": (
                "Las alineaciones todavía "
                "no han sido publicadas."
            )
        }

    # --------------------------------------------------------
    # 5. OBTENER ESTADÍSTICAS DE LOS EQUIPOS
    # --------------------------------------------------------

    home_stats_result = (
        await obtener_estadisticas_equipo(
            home_team.get("id"),
            CURRENT_SEASON
        )
    )

    away_stats_result = (
        await obtener_estadisticas_equipo(
            away_team.get("id"),
            CURRENT_SEASON
        )
    )

    season_players = []

    if home_stats_result["ok"]:

        season_players.extend(
            home_stats_result["players"]
        )

    if away_stats_result["ok"]:

        season_players.extend(
            away_stats_result["players"]
        )

    # --------------------------------------------------------
    # 6. ÍNDICE DE ESTADÍSTICAS
    # --------------------------------------------------------

    stats_by_player = {}

    for player in season_players:

        player_id = player.get(
            "player_id"
        )

        if player_id:

            stats_by_player[
                player_id
            ] = player

    # --------------------------------------------------------
    # 7. CRUZAR ALINEACIONES Y ESTADÍSTICAS
    # --------------------------------------------------------

    players = []

    for lineup_player in lineups:

        player_id = lineup_player.get(
            "player_id"
        )

        stats = stats_by_player.get(
            player_id
        )

        # ----------------------------------------------------
        # Si el jugador no apareció en la primera página
        # del equipo, intentamos consulta directa.
        # ----------------------------------------------------

        if not stats:

            stats = await obtener_estadisticas_jugador(
                player_id,
                CURRENT_SEASON
            )

        if not stats:

            stats = {}

        merged = {
            **stats,
            **lineup_player
        }

        merged["player_id"] = (
            player_id
        )

        merged["confirmed_lineup"] = (
            True
        )

        merged["starter"] = (
            lineup_player.get(
                "starter",
                False
            )
        )

        merged["fixture_id"] = (
            fixture_id
        )

        merged["data_source"] = (
            "API-Football"
        )

        merged["season"] = (
            CURRENT_SEASON
        )

        players.append(
            merged
        )

    # --------------------------------------------------------
    # 8. TITULARES
    # --------------------------------------------------------

    starters = [
        player
        for player in players
        if player.get("starter")
    ]

    # --------------------------------------------------------
    # 9. SUPLENTES
    # --------------------------------------------------------

    substitutes = [
        player
        for player in players
        if not player.get("starter")
    ]

    # --------------------------------------------------------
    # 10. ESTADO DE DATOS
    # --------------------------------------------------------

    players_with_stats = [
        player
        for player in players
        if player.get("shots") is not None
    ]

    # --------------------------------------------------------
    # 11. RESPUESTA
    # --------------------------------------------------------

    return {

        "status":
            "real_player_data",

        "available":
            True,

        "fixture_id":
            fixture_id,

        "fixture": {

            "date":
                fixture_data.get(
                    "fixture",
                    {}
                ).get(
                    "date"
                ),

            "status":
                fixture_data.get(
                    "fixture",
                    {}
                ).get(
                    "status",
                    {}
                ),

            "league":
                fixture_data.get(
                    "league",
                    {}
                ).get(
                    "name"
                ),

            "country":
                fixture_data.get(
                    "league",
                    {}
                ).get(
                    "country"
                ),

            "home":
                home_team,

            "away":
                away_team
        },

        "count":
            len(players),

        "starters_count":
            len(starters),

        "substitutes_count":
            len(substitutes),

        "players_with_stats":
            len(players_with_stats),

        "starters":
            starters,

        "substitutes":
            substitutes,

        "players":
            players,

        "markets":
            SUPPORTED_MARKETS,

        "senal":
            "DATOS REALES",

        "signal":
            "DATOS REALES",

        "motor":
            "Fútbol Analytics V1.5",

        "message": (
            "Alineaciones confirmadas y "
            "estadísticas de temporada "
            "obtenidas desde API-Football."
        )
    }


# ============================================================
# PLAYER MARKETS - MÚLTIPLES
# ============================================================

@app.get("/api/player-markets")
async def player_markets(
    fixture_id: Optional[int] = None
):

    result = await player_market(
        fixture_id=fixture_id
    )

    if not result.get(
        "available",
        False
    ):

        return {

            "status":
                result.get(
                    "status",
                    "waiting_data"
                ),

            "available":
                False,

            "count":
                0,

            "markets":
                [],

            "players":
                [],

            "message":
                result.get(
                    "message",
                    "Sin datos reales."
                ),

            "motor":
                "Fútbol Analytics V1.5"
        }

    return {

        "status":
            "real_player_data",

        "available":
            True,

        "count":
            result.get(
                "count",
                0
            ),

        "starters_count":
            result.get(
                "starters_count",
                0
            ),

        "markets":
            SUPPORTED_MARKETS,

        "players":
            result.get(
                "players",
                []
            ),

        "starters":
            result.get(
                "starters",
                []
            ),

        "substitutes":
            result.get(
                "substitutes",
                []
            ),

        "fixture_id":
            result.get(
                "fixture_id"
            ),

        "message": (
            "Datos reales de jugadores "
            "disponibles. Las cuotas y "
            "líneas deben conectarse "
            "antes de emitir picks."
        ),

        "motor":
            "Fútbol Analytics V1.5"
    }


# ============================================================
# BANKROLL
# ============================================================

@app.post("/api/bankroll")
def bankroll_calculator(
    request: BankrollRequest
):

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            request.stake_percent,
            MAX_STAKE_PERCENT
        )
    )

    stake = calcular_stake(
        request.bankroll,
        stake_percent
    )

    return {

        "bankroll":
            request.bankroll,

        "stake_percent":
            stake_percent,

        "recommended_stake":
            stake,

        "minimum_stake":
            calcular_stake(
                request.bankroll,
                MIN_STAKE_PERCENT
            ),

        "maximum_stake":
            calcular_stake(
                request.bankroll,
                MAX_STAKE_PERCENT
            ),

        "all_in_allowed":
            False
    }


# ============================================================
# BET TRACKER
# ============================================================

@app.post("/api/bets")
def create_bet(
    request: BetRequest
):

    bet = {

        "id":
            len(bets) + 1,

        "event":
            request.event,

        "market":
            request.market,

        "odds":
            request.odds,

        "stake":
            request.stake,

        "result":
            request.result,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat()
    }

    bets.append(
        bet
    )

    return {

        "status":
            "created",

        "bet":
            bet
    }


@app.get("/api/bets")
def get_bets():

    return {

        "count":
            len(bets),

        "bets":
            bets
    }


# ============================================================
# KPIs
# ============================================================

@app.get("/api/kpis")
def get_kpis():

    total_bets = len(
        bets
    )

    settled_bets = [
        bet
        for bet in bets
        if bet["result"] in [
            "GANADA",
            "PERDIDA",
            "ANULADA"
        ]
    ]

    total_staked = sum(
        bet["stake"]
        for bet in settled_bets
    )

    profit = 0

    wins = 0

    losses = 0

    voids = 0

    for bet in settled_bets:

        stake = bet[
            "stake"
        ]

        odds = bet[
            "odds"
        ]

        result = bet[
            "result"
        ]

        if result == "GANADA":

            profit += (
                stake
                * (odds - 1)
            )

            wins += 1

        elif result == "PERDIDA":

            profit -= stake

            losses += 1

        elif result == "ANULADA":

            voids += 1

    yield_percent = (

        (
            profit
            / total_staked
        ) * 100

        if total_staked > 0

        else 0
    )

    win_rate = (

        (
            wins
            / (wins + losses)
        ) * 100

        if wins + losses > 0

        else 0
    )

    return {

        "total_bets":
            total_bets,

        "settled_bets":
            len(
                settled_bets
            ),

        "profit":
            round(
                profit,
                2
            ),

        "yield":
            round(
                yield_percent,
                2
            ),

        "win_rate":
            round(
                win_rate,
                2
            ),

        "wins":
            wins,

        "losses":
            losses,

        "voids":
            voids
    }
