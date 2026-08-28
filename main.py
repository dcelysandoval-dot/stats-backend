import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
import math

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

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
BASE_URL = "https://api.football-data.org/v4"
FREE_LEAGUES = "PL,PD,SA,BL1,FL1,CL,EC,WC"


# ============================================================
# CONFIGURACIÓN API-FOOTBALL / API-SPORTS
# ============================================================

APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "")
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
    "Tarjetas",
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
# UTILIDADES
# ============================================================

def safe_number(value: Any) -> float:
    if value is None:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    try:
        text = str(value).replace("%", "").strip()
        return float(text)
    except Exception:
        return 0.0


def safe_int(value: Any) -> int:
    return int(round(safe_number(value)))


def per90(value: float, minutes: float) -> float:
    if minutes <= 0:
        return 0.0

    return round(
        value * 90.0 / minutes,
        3,
    )


# ============================================================
# MOTOR MATEMÁTICO
# ============================================================

def calcular_indice_ivj(match: dict) -> float:

    score = match.get(
        "score",
        {},
    ).get(
        "fullTime",
        {},
    )

    home_score = score.get(
        "home",
        0,
    ) or 0

    away_score = score.get(
        "away",
        0,
    ) or 0

    ivj_base = (
        5.0
        + (home_score + away_score) * 0.5
    )

    return round(
        ivj_base,
        2,
    )


def probabilidad_implicita(
    cuota: float,
) -> float:

    if cuota <= 1:
        return 0.0

    return round(
        (1 / cuota) * 100,
        2,
    )


def calcular_value_edge(
    probabilidad_fa: float,
    cuota: float,
) -> float:

    probabilidad_cuota = (
        probabilidad_implicita(cuota)
    )

    return round(
        probabilidad_fa - probabilidad_cuota,
        2,
    )


def calcular_fa_rating(
    probabilidad_fa: float,
    value_edge: float,
    confianza: float = 70,
) -> int:

    score = (
        probabilidad_fa * 0.45
        + max(value_edge, 0) * 1.5
        + confianza * 0.25
    )

    score = max(
        0,
        min(score, 100),
    )

    return round(score)


def calcular_stake(
    bankroll: float,
    stake_percent: float,
) -> float:

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            stake_percent,
            MAX_STAKE_PERCENT,
        ),
    )

    return round(
        bankroll * stake_percent / 100,
        2,
    )


def determinar_riesgo(
    fa_rating: int,
) -> str:

    if fa_rating >= 80:
        return "BAJO"

    if fa_rating >= 70:
        return "MEDIO"

    if fa_rating >= 60:
        return "MODERADO"

    return "ALTO"


def determinar_senal(
    value_edge: float,
) -> str:

    if value_edge >= 15:
        return "OPORTUNIDAD ALTA"

    if value_edge >= 8:
        return "OPORTUNIDAD MEDIA"

    if value_edge > 0:
        return "VALOR BAJO"

    return "SIN VALOR"


# ============================================================
# CLIENTE API-FOOTBALL
# ============================================================

async def apifootball_get(
    endpoint: str,
    params: Optional[dict] = None,
):

    if not APIFOOTBALL_KEY:

        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": (
                "APIFOOTBALL_KEY no está "
                "configurada en Render."
            ),
        }

    headers = {
        "x-apisports-key": APIFOOTBALL_KEY,
    }

    url = (
        APIFOOTBALL_URL
        + endpoint
    )

    try:

        async with httpx.AsyncClient(
            timeout=25.0,
        ) as client:

            response = await client.get(
                url,
                headers=headers,
                params=params or {},
            )

            try:
                data = response.json()
            except Exception:
                data = {
                    "raw": response.text,
                }

            api_errors = (
                data.get("errors", [])
                if isinstance(data, dict)
                else []
            )

            if response.status_code != 200:

                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "data": data,
                    "error": (
                        "API-Football respondió "
                        f"HTTP {response.status_code}"
                    ),
                }

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
            "error": str(exc),
        }

    except Exception as exc:

        return {
            "ok": False,
            "status_code": None,
            "data": None,
            "error": str(exc),
        }


# ============================================================
# NORMALIZADOR DE ESTADÍSTICAS DE PARTIDO
# ============================================================

def normalizar_player_statistics(
    player_block: dict,
) -> dict:

    player = (
        player_block.get(
            "player",
            {},
        )
        or {}
    )

    statistics = (
        player_block.get(
            "statistics",
            [],
        )
        or []
    )

    stats = (
        statistics[0]
        if statistics
        else {}
    ) or {}

    shots = (
        stats.get(
            "shots",
            {},
        )
        or {}
    )

    goals = (
        stats.get(
            "goals",
            {},
        )
        or {}
    )

    passes = (
        stats.get(
            "passes",
            {},
        )
        or {}
    )

    fouls = (
        stats.get(
            "fouls",
            {},
        )
        or {}
    )

    cards = (
        stats.get(
            "cards",
            {},
        )
        or {}
    )

    games = (
        stats.get(
            "games",
            {},
        )
        or {}
    )

    team = (
        player_block.get(
            "team",
            {},
        )
        or {}
    )

    return {

        "player_id":
            player.get("id"),

        "player":
            player.get("name"),

        "photo":
            player.get("photo"),

        "team":
            team.get("name"),

        "team_id":
            team.get("id"),

        "position":
            games.get("position"),

        "minutes":
            safe_number(
                games.get("minutes"),
            ),

        "rating":
            safe_number(
                games.get("rating"),
            ),

        "starter":
            games.get("substitute")
            is False,

        "shots":
            safe_number(
                shots.get("total"),
            ),

        "shots_on_target":
            safe_number(
                shots.get("on"),
            ),

        "goals":
            safe_number(
                goals.get("total"),
            ),

        "assists":
            safe_number(
                goals.get("assists"),
            ),

        "key_passes":
            safe_number(
                passes.get("key"),
            ),

        "fouls_committed":
            safe_number(
                fouls.get("committed"),
            ),

        "fouls_drawn":
            safe_number(
                fouls.get("drawn"),
            ),

        "yellow_cards":
            safe_number(
                cards.get("yellow"),
            ),

        "red_cards":
            safe_number(
                cards.get("red"),
            ),
    }


# ============================================================
# NORMALIZADOR DE ESTADÍSTICAS DE TEMPORADA
# ============================================================

def normalize_season_player(
    item: dict,
) -> dict:

    player = (
        item.get(
            "player",
            {},
        )
        or {}
    )

    stats_list = (
        item.get(
            "statistics",
            [],
        )
        or []
    )

    totals = {

        "minutes": 0.0,

        "shots": 0.0,

        "shots_on_target": 0.0,

        "goals": 0.0,

        "assists": 0.0,

        "key_passes": 0.0,

        "fouls_committed": 0.0,

        "fouls_drawn": 0.0,

        "yellow_cards": 0.0,

        "red_cards": 0.0,

        "appearances": 0.0,

        "starts": 0.0,

        "ratings_sum": 0.0,

        "ratings_count": 0.0,
    }

    teams = []

    leagues = []

    for stat in stats_list:

        games = (
            stat.get(
                "games",
                {},
            )
            or {}
        )

        shots = (
            stat.get(
                "shots",
                {},
            )
            or {}
        )

        goals = (
            stat.get(
                "goals",
                {},
            )
            or {}
        )

        passes = (
            stat.get(
                "passes",
                {},
            )
            or {}
        )

        fouls = (
            stat.get(
                "fouls",
                {},
            )
            or {}
        )

        cards = (
            stat.get(
                "cards",
                {},
            )
            or {}
        )

        team = (
            stat.get(
                "team",
                {},
            )
            or {}
        )

        league = (
            stat.get(
                "league",
                {},
            )
            or {}
        )

        minutes = safe_number(
            games.get("minutes"),
        )

        appearances = safe_number(
            games.get("appearences"),
        )

        starts = safe_number(
            games.get("lineups"),
        )

        totals["minutes"] += minutes

        totals["appearances"] += (
            appearances
        )

        totals["starts"] += (
            starts
        )

        totals["shots"] += (
            safe_number(
                shots.get("total"),
            )
        )

        totals["shots_on_target"] += (
            safe_number(
                shots.get("on"),
            )
        )

        totals["goals"] += (
            safe_number(
                goals.get("total"),
            )
        )

        totals["assists"] += (
            safe_number(
                goals.get("assists"),
            )
        )

        totals["key_passes"] += (
            safe_number(
                passes.get("key"),
            )
        )

        totals["fouls_committed"] += (
            safe_number(
                fouls.get("committed"),
            )
        )

        totals["fouls_drawn"] += (
            safe_number(
                fouls.get("drawn"),
            )
        )

        totals["yellow_cards"] += (
            safe_number(
                cards.get("yellow"),
            )
        )

        totals["red_cards"] += (
            safe_number(
                cards.get("red"),
            )
        )

        rating = safe_number(
            games.get("rating"),
        )

        if rating > 0:

            totals["ratings_sum"] += (
                rating
            )

            totals["ratings_count"] += 1

        if team.get("id") is not None:

            teams.append({
                "id":
                    team.get("id"),

                "name":
                    team.get("name"),
            })

        if league.get("id") is not None:

            leagues.append({
                "id":
                    league.get("id"),

                "name":
                    league.get("name"),
            })

    minutes = totals["minutes"]

    return {

        "player_id":
            player.get("id"),

        "player":
            player.get("name"),

        "photo":
            player.get("photo"),

        "age":
            player.get("age"),

        "nationality":
            player.get("nationality"),

        "minutes":
            round(
                minutes,
                2,
            ),

        "appearances":
            safe_int(
                totals["appearances"],
            ),

        "starts":
            safe_int(
                totals["starts"],
            ),

        "shots":
            round(
                totals["shots"],
                2,
            ),

        "shots_on_target":
            round(
                totals["shots_on_target"],
                2,
            ),

        "goals":
            round(
                totals["goals"],
                2,
            ),

        "assists":
            round(
                totals["assists"],
                2,
            ),

        "key_passes":
            round(
                totals["key_passes"],
                2,
            ),

        "fouls_committed":
            round(
                totals["fouls_committed"],
                2,
            ),

        "fouls_drawn":
            round(
                totals["fouls_drawn"],
                2,
            ),

        "yellow_cards":
            safe_int(
                totals["yellow_cards"],
            ),

        "red_cards":
            safe_int(
                totals["red_cards"],
            ),

        "rating_avg":
            round(
                totals["ratings_sum"]
                / totals["ratings_count"],
                2,
            )
            if totals["ratings_count"]
            else 0,

        "shots_per90":
            per90(
                totals["shots"],
                minutes,
            ),

        "shots_on_target_per90":
            per90(
                totals["shots_on_target"],
                minutes,
            ),

        "goals_per90":
            per90(
                totals["goals"],
                minutes,
            ),

        "assists_per90":
            per90(
                totals["assists"],
                minutes,
            ),

        "key_passes_per90":
            per90(
                totals["key_passes"],
                minutes,
            ),

        "fouls_committed_per90":
            per90(
                totals["fouls_committed"],
                minutes,
            ),

        "fouls_drawn_per90":
            per90(
                totals["fouls_drawn"],
                minutes,
            ),

        "teams":
            teams,

        "leagues":
            leagues,
    }


# ============================================================
# FORMA RECIENTE
# ============================================================

def build_recent_form_from_fixture_data(
    fixture_data: dict,
    player_ids: set[int],
    last_n: int = 5,
) -> dict[int, dict]:

    recent = {}

    response = (
        fixture_data.get(
            "response",
            [],
        )
        if isinstance(
            fixture_data,
            dict,
        )
        else []
    )

    for fixture in response:

        fixture_info = (
            fixture.get(
                "fixture",
                {},
            )
            or {}
        )

        fixture_id = (
            fixture_info.get(
                "id",
            )
        )

        date = (
            fixture_info.get(
                "date",
            )
        )

        status = (
            fixture_info.get(
                "status",
                {},
            )
            or {}
        ).get(
            "short",
        )

        for team_block in (
            fixture.get(
                "players",
                [],
            )
            or []
        ):

            for player_block in (
                team_block.get(
                    "players",
                    [],
                )
                or []
            ):

                pid = (
                    player_block.get(
                        "player",
                        {},
                    )
                    or {}
                ).get(
                    "id",
                )

                if pid not in player_ids:
                    continue

                row = (
                    normalizar_player_statistics(
                        player_block,
                    )
                )

                row["fixture_id"] = (
                    fixture_id
                )

                row["date"] = date

                row["status"] = status

                recent.setdefault(
                    pid,
                    [],
                ).append(
                    row
                )

    for pid in recent:

        recent[pid] = sorted(
            recent[pid],
            key=lambda x:
                x.get("date") or "",
            reverse=True,
        )[:last_n]

    return recent


def aggregate_recent_form(
    rows: list[dict],
) -> dict:

    if not rows:

        return {

            "matches": 0,

            "minutes": 0,

            "shots": 0,

            "shots_on_target": 0,

            "goals": 0,

            "assists": 0,

            "fouls_committed": 0,

            "yellow_cards": 0,

            "rating_avg": 0,

            "shots_per90": 0,

            "shots_on_target_per90": 0,

            "goals_per90": 0,

            "assists_per90": 0,
        }

    totals = {

        "minutes": 0.0,

        "shots": 0.0,

        "shots_on_target": 0.0,

        "goals": 0.0,

        "assists": 0.0,

        "fouls_committed": 0.0,

        "yellow_cards": 0.0,

        "rating_sum": 0.0,

        "rating_count": 0,
    }

    for row in rows:

        totals["minutes"] += (
            safe_number(
                row.get("minutes"),
            )
        )

        totals["shots"] += (
            safe_number(
                row.get("shots"),
            )
        )

        totals["shots_on_target"] += (
            safe_number(
                row.get(
                    "shots_on_target",
                )
            )
        )

        totals["goals"] += (
            safe_number(
                row.get("goals"),
            )
        )

        totals["assists"] += (
            safe_number(
                row.get("assists"),
            )
        )

        totals["fouls_committed"] += (
            safe_number(
                row.get(
                    "fouls_committed",
                )
            )
        )

        totals["yellow_cards"] += (
            safe_number(
                row.get(
                    "yellow_cards",
                )
            )
        )

        rating = safe_number(
            row.get("rating"),
        )

        if rating:

            totals["rating_sum"] += (
                rating
            )

            totals["rating_count"] += 1

    minutes = totals["minutes"]

    return {

        "matches":
            len(rows),

        "minutes":
            round(
                minutes,
                2,
            ),

        "shots":
            round(
                totals["shots"],
                2,
            ),

        "shots_on_target":
            round(
                totals["shots_on_target"],
                2,
            ),

        "goals":
            round(
                totals["goals"],
                2,
            ),

        "assists":
            round(
                totals["assists"],
                2,
            ),

        "fouls_committed":
            round(
                totals["fouls_committed"],
                2,
            ),

        "yellow_cards":
            safe_int(
                totals["yellow_cards"],
            ),

        "rating_avg":
            round(
                totals["rating_sum"]
                / totals["rating_count"],
                2,
            )
            if totals["rating_count"]
            else 0,

        "shots_per90":
            per90(
                totals["shots"],
                minutes,
            ),

        "shots_on_target_per90":
            per90(
                totals["shots_on_target"],
                minutes,
            ),

        "goals_per90":
            per90(
                totals["goals"],
                minutes,
            ),

        "assists_per90":
            per90(
                totals["assists"],
                minutes,
            ),
    }


# ============================================================
# PROYECCIONES
# ============================================================

def estimate_projection(
    season: dict,
    recent: dict,
    starter: bool,
    market: str,
    expected_minutes: Optional[float] = None,
) -> dict:

    season_minutes = safe_number(
        season.get("minutes"),
    )

    recent_minutes = safe_number(
        recent.get("minutes"),
    )

    if expected_minutes is None:

        if starter:

            expected_minutes = 75.0

        elif recent_minutes > 0:

            expected_minutes = min(
                60.0,
                recent_minutes
                / max(
                    recent.get(
                        "matches",
                        1,
                    ),
                    1,
                ),
            )

        else:

            expected_minutes = 45.0

    expected_minutes = max(
        20.0,
        min(
            expected_minutes,
            90.0,
        ),
    )

    def blend(
        key: str,
    ) -> float:

        season_value = safe_number(
            season.get(key),
        )

        recent_value = safe_number(
            recent.get(key),
        )

        if recent_value > 0:

            return round(
                season_value * 0.60
                + recent_value * 0.40,
                3,
            )

        return round(
            season_value,
            3,
        )

    if market == "Remates":

        rate = blend(
            "shots_per90",
        )

    elif market == "Remates a puerta":

        rate = blend(
            "shots_on_target_per90",
        )

    elif market == "Goles":

        rate = blend(
            "goals_per90",
        )

    elif market == "Asistencias":

        rate = blend(
            "assists_per90",
        )

    elif market == "Faltas":

        rate = blend(
            "fouls_committed_per90",
        )

    elif market == "Tarjetas":

        rate = per90(
            safe_number(
                season.get(
                    "yellow_cards",
                )
            ),
            season_minutes,
        )

    else:

        rate = 0.0

    projection = (
        rate
        * expected_minutes
        / 90.0
    )

    return {

        "expected_minutes":
            round(
                expected_minutes,
                1,
            ),

        "rate_per90":
            round(
                rate,
                3,
            ),

        "projection":
            round(
                projection,
                3,
            ),
    }


def poisson_probability_over(
    lam: float,
    line: float,
) -> float:

    if lam <= 0:
        return 0.0

    threshold = (
        math.floor(line)
        + 1
    )

    cumulative = 0.0

    base = math.exp(-lam)

    for k in range(
        threshold,
    ):

        term_k = (
            base
            if k == 0
            else (
                base
                * (lam ** k)
                / math.factorial(k)
            )
        )

        cumulative += term_k

    return round(
        max(
            0.0,
            min(
                100.0,
                (
                    1.0
                    - cumulative
                )
                * 100.0,
            ),
        ),
        2,
    )


def market_signal_from_projection(
    projection: float,
    line: float,
) -> str:

    if projection <= 0 or line < 0:
        return "SIN PROYECCIÓN"

    ratio = (
        projection
        / max(
            line,
            0.5,
        )
    )

    if ratio >= 1.35:
        return "MUY FUERTE"

    if ratio >= 1.15:
        return "FUERTE"

    if ratio >= 1.02:
        return "LEVE"

    return "SIN VENTAJA"


# ============================================================
# MODELOS
# ============================================================

class BankrollRequest(
    BaseModel,
):

    bankroll: float

    stake_percent: float = (
        DEFAULT_STAKE_PERCENT
    )


class ScannerRequest(
    BaseModel,
):

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


class BetRequest(
    BaseModel,
):

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

        "status":
            "ok",

        "project":
            "Fútbol Analytics",

        "version":
            "1.5.0",

        "engine":
            "Player Market Scanner V1.5",

        "api_football":
            bool(
                APIFOOTBALL_KEY
            ),

        "message":
            "Fútbol Analytics API corriendo",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {

        "status":
            "healthy",

        "service":
            "futbol-analytics-backend",

        "version":
            "1.5.0",

        "api_football_configured":
            bool(
                APIFOOTBALL_KEY
            ),
    }


# ============================================================
# TEST API-FOOTBALL
# ============================================================

@app.get(
    "/api/test-apifootball",
)
async def test_apifootball():

    result = await apifootball_get(
        "/status",
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "api_configured":
                bool(
                    APIFOOTBALL_KEY
                ),

            "status_code":
                result[
                    "status_code"
                ],

            "message":
                result[
                    "error"
                ],

            "details":
                result[
                    "data"
                ],
        }

    return {

        "status":
            "connected",

        "api_configured":
            True,

        "status_code":
            result[
                "status_code"
            ],

        "api_response":
            result[
                "data"
            ],
    }


# ============================================================
# PARTIDOS FOOTBALL-DATA.ORG
# ============================================================

@app.get("/api/matches")
async def get_matches(
    days_ahead: int = 1,
):

    if not FOOTBALL_DATA_API_KEY:

        return {

            "count":
                0,

            "matches":
                [],

            "error":
                (
                    "FOOTBALL_DATA_API_KEY "
                    "no está configurada."
                ),
        }

    headers = {
        "X-Auth-Token":
            FOOTBALL_DATA_API_KEY,
    }

    now_utc = datetime.now(
        timezone.utc,
    )

    date_from = (
        now_utc.strftime(
            "%Y-%m-%d",
        )
    )

    date_to = (
        now_utc
        + timedelta(
            days=days_ahead,
        )
    ).strftime(
        "%Y-%m-%d",
    )

    url = (
        f"{BASE_URL}/matches"
        f"?dateFrom={date_from}"
        f"&dateTo={date_to}"
        f"&competitions={FREE_LEAGUES}"
    )

    try:

        async with httpx.AsyncClient(
            timeout=15.0,
        ) as client:

            response = await client.get(
                url,
                headers=headers,
            )

            if response.status_code != 200:

                try:
                    details = (
                        response.json()
                    )
                except Exception:
                    details = (
                        response.text
                    )

                return {

                    "count":
                        0,

                    "matches":
                        [],

                    "error":
                        (
                            "Error en API externa "
                            f"({response.status_code})"
                        ),

                    "details":
                        details,
                }

            data = response.json()

            processed_matches = []

            for match in data.get(
                "matches",
                [],
            ):

                competition = (
                    match.get(
                        "competition",
                        {},
                    )
                    or {}
                )

                home_team = (
                    match.get(
                        "homeTeam",
                        {},
                    )
                    or {}
                )

                away_team = (
                    match.get(
                        "awayTeam",
                        {},
                    )
                    or {}
                )

                full_time = (
                    match.get(
                        "score",
                        {},
                    )
                    .get(
                        "fullTime",
                        {},
                    )
                )

                processed_matches.append({

                    "id":
                        match.get(
                            "id",
                        ),

                    "utcDate":
                        match.get(
                            "utcDate",
                        ),

                    "status":
                        match.get(
                            "status",
                        ),

                    "competition": {

                        "name":
                            competition.get(
                                "name",
                            ),

                        "emblem":
                            competition.get(
                                "emblem",
                            ),
                    },

                    "homeTeam": {

                        "name":
                            home_team.get(
                                "name",
                            ),

                        "crest":
                            home_team.get(
                                "crest",
                            ),
                    },

                    "awayTeam": {

                        "name":
                            away_team.get(
                                "name",
                            ),

                        "crest":
                            away_team.get(
                                "crest",
                            ),
                    },

                    "score":
                        full_time,

                    "ivjIndex":
                        calcular_indice_ivj(
                            match,
                        ),
                })

            return {

                "count":
                    len(
                        processed_matches,
                    ),

                "dateFrom":
                    date_from,

                "dateTo":
                    date_to,

                "matches":
                    processed_matches,
            }

    except Exception as exc:

        return {

            "count":
                0,

            "matches":
                [],

            "error":
                "Error procesando partidos",

            "details":
                str(exc),
        }


# ============================================================
# COMPATIBILIDAD FRONTEND
# ============================================================

@app.get("/api/partidos-hoy")
async def partidos_hoy():

    return await get_matches(
        days_ahead=1,
    )


# ============================================================
# API-FOOTBALL - FIXTURES
# ============================================================

@app.get(
    "/api/apifootball-fixtures",
)
async def apifootball_fixtures(
    date: Optional[str] = None,
    league: Optional[int] = None,
    season: int = CURRENT_SEASON,
    next: int = 10,
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
                timezone.utc,
            ).strftime(
                "%Y-%m-%d",
            )
        )

    result = await apifootball_get(
        "/fixtures",
        params,
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
                result[
                    "error"
                ],

            "details":
                result[
                    "data"
                ],
        }

    fixtures = []

    for fixture in result[
        "data"
    ].get(
        "response",
        [],
    ):

        fixture_info = (
            fixture.get(
                "fixture",
                {},
            )
            or {}
        )

        teams = (
            fixture.get(
                "teams",
                {},
            )
            or {}
        )

        league_info = (
            fixture.get(
                "league",
                {},
            )
            or {}
        )

        home = (
            teams.get(
                "home",
                {},
            )
            or {}
        )

        away = (
            teams.get(
                "away",
                {},
            )
            or {}
        )

        fixtures.append({

            "fixture_id":
                fixture_info.get(
                    "id",
                ),

            "date":
                fixture_info.get(
                    "date",
                ),

            "status":
                fixture_info.get(
                    "status",
                    {},
                ),

            "league": {

                "id":
                    league_info.get(
                        "id",
                    ),

                "name":
                    league_info.get(
                        "name",
                    ),

                "country":
                    league_info.get(
                        "country",
                    ),
            },

            "home": {

                "id":
                    home.get(
                        "id",
                    ),

                "name":
                    home.get(
                        "name",
                    ),

                "logo":
                    home.get(
                        "logo",
                    ),
            },

            "away": {

                "id":
                    away.get(
                        "id",
                    ),

                "name":
                    away.get(
                        "name",
                    ),

                "logo":
                    away.get(
                        "logo",
                    ),
            },
        })

    return {

        "status":
            "ok",

        "count":
            len(
                fixtures,
            ),

        "fixtures":
            fixtures,

        "source":
            "API-Football",

        "season":
            season,
    }


# ============================================================
# API-FOOTBALL - JUGADORES DE UN FIXTURE
# ============================================================

@app.get("/api/fixture-players")
async def fixture_players(
    fixture_id: int,
):

    result = await apifootball_get(
        "/fixtures/players",
        {
            "fixture":
                fixture_id,
        },
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
                result[
                    "error"
                ],

            "details":
                result[
                    "data"
                ],
        }

    players = []

    for team_block in result[
        "data"
    ].get(
        "response",
        [],
    ):

        team_info = (
            team_block.get(
                "team",
                {},
            )
            or {}
        )

        for player_block in (
            team_block.get(
                "players",
                [],
            )
        ):

            normalized = (
                normalizar_player_statistics(
                    player_block,
                )
            )

            normalized[
                "team"
            ] = team_info.get(
                "name",
            )

            normalized[
                "team_id"
            ] = team_info.get(
                "id",
            )

            players.append(
                normalized,
            )

    return {

        "status":
            "ok",

        "fixture_id":
            fixture_id,

        "count":
            len(
                players,
            ),

        "players":
            players,

        "source":
            "API-Football",
    }


# ============================================================
# API-FOOTBALL - BUSCAR JUGADOR
# ============================================================

@app.get("/api/player-search")
async def player_search(
    search: str,
    season: int = CURRENT_SEASON,
):

    result = await apifootball_get(
        "/players",
        {
            "search":
                search,

            "season":
                season,
        },
    )

    if not result["ok"]:

        return {

            "status":
                "error",

            "players":
                [],

            "message":
                result[
                    "error"
                ],

            "details":
                result[
                    "data"
                ],
        }

    players = []

    for item in result[
        "data"
    ].get(
        "response",
        [],
    ):

        player = (
            item.get(
                "player",
                {},
            )
            or {}
        )

        players.append({

            "id":
                player.get(
                    "id",
                ),

            "name":
                player.get(
                    "name",
                ),

            "firstname":
                player.get(
                    "firstname",
                ),

            "lastname":
                player.get(
                    "lastname",
                ),

            "age":
                player.get(
                    "age",
                ),

            "nationality":
                player.get(
                    "nationality",
                ),

            "photo":
                player.get(
                    "photo",
                ),
        })

    return {

        "status":
            "ok",

        "count":
            len(
                players,
            ),

        "players":
            players,

        "source":
            "API-Football",
    }


# ============================================================
# API-FOOTBALL - ESTADÍSTICAS DE TEMPORADA
# ============================================================

@app.get("/api/player-season")
async def player_season(
    player_id: int,
    season: int = CURRENT_SEASON,
):

    result = await apifootball_get(
        "/players",
        {
            "id":
                player_id,

            "season":
                season,
        },
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
                result[
                    "error"
                ],

            "details":
                result[
                    "data"
                ],
        }

    response = result[
        "data"
    ].get(
        "response",
        [],
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
                (
                    "No existen estadísticas "
                    "para este jugador."
                ),
        }

    player = (
        response[0].get(
            "player",
            {},
        )
        or {}
    )

    return {

        "status":
            "ok",

        "player": {

            "id":
                player.get(
                    "id",
                ),

            "name":
                player.get(
                    "name",
                ),

            "photo":
                player.get(
                    "photo",
                ),
        },

        "season":
            season,

        "statistics":
            response[0].get(
                "statistics",
                [],
            ),

        "source":
            "API-Football",
    }


# ============================================================
# FUNCIONES PARA CONSTRUIR EL SCANNER
# ============================================================

async def get_team_season_players(
    team_id: int,
    season: int,
) -> dict[int, dict]:

    if not team_id:
        return {}

    result = await apifootball_get(
        "/players",
        {
            "team":
                team_id,

            "season":
                season,
        },
    )

    if not result["ok"]:
        return {}

    mapped = {}

    for item in result[
        "data"
    ].get(
        "response",
        [],
    ):

        normalized = (
            normalize_season_player(
                item,
            )
        )

        if normalized.get(
            "player_id",
        ) is not None:

            mapped[
                normalized[
                    "player_id"
                ]
            ] = normalized

    return mapped


async def get_team_last_fixture_ids(
    team_id: int,
    last: int = 5,
) -> list[int]:

    if not team_id:
        return []

    result = await apifootball_get(
        "/fixtures",
        {
            "team":
                team_id,

            "last":
                last,
        },
    )

    if not result["ok"]:
        return []

    ids = []

    for item in result[
        "data"
    ].get(
        "response",
        [],
    ):

        fixture = (
            item.get(
                "fixture",
                {},
            )
            or {}
        )

        fixture_id = fixture.get(
            "id",
        )

        status = (
            fixture.get(
                "status",
                {},
            )
            or {}
        ).get(
            "short",
        )

        if (
            fixture_id
            and status in {
                "FT",
                "AET",
                "PEN",
            }
        ):

            ids.append(
                fixture_id,
            )

    return ids[:last]


async def get_fixtures_batch(
    ids: list[int],
) -> dict:

    if not ids:

        return {
            "response":
                [],
        }

    # API-Football allows multiple fixture IDs in one request.
    result = await apifootball_get(
        "/fixtures",
        {
            "ids":
                "-".join(
                    map(
                        str,
                        ids,
                    )
                ),
        },
    )

    if not result["ok"]:

        return {
            "response":
                [],
        }

    return (
        result["data"]
        or {
            "response":
                [],
        }
    )


async def get_confirmed_fixture_players(
    fixture_id: int,
) -> tuple[dict, list[dict]]:

    result = await apifootball_get(
        "/fixtures/players",
        {
            "fixture":
                fixture_id,
        },
    )

    if not result["ok"]:

        return (
            {},
            [],
        )

    players = []

    fixture_meta = {}

    for team_block in result[
        "data"
    ].get(
        "response",
        [],
    ):

        team_info = (
            team_block.get(
                "team",
                {},
            )
            or {}
        )

        for player_block in (
            team_block.get(
                "players",
                [],
            )
        ):

            row = (
                normalizar_player_statistics(
                    player_block,
                )
            )

            row["team"] = (
                team_info.get(
                    "name",
                )
            )

            row["team_id"] = (
                team_info.get(
                    "id",
                )
            )

            row["number"] = (
                player_block.get(
                    "number",
                )
            )

            row["grid"] = (
                player_block.get(
                    "grid",
                )
            )

            # In the pre-match lineup response,
            # "starter" is the important field.
            row["confirmed_lineup"] = (
                player_block.get(
                    "starter",
                )
                is not None
            )

            row["starter"] = (
                player_block.get(
                    "starter",
                )
                is True
            )

            row["fixture_id"] = (
                fixture_id
            )

            row["data_source"] = (
                "API-Football"
            )

            row["season"] = (
                CURRENT_SEASON
            )

            players.append(
                row,
            )

        fixture_meta[
            team_info.get(
                "id",
            )
        ] = team_info.get(
            "name",
        )

    return (
        fixture_meta,
        players,
    )


# ============================================================
# PLAYER MARKET SCANNER V1.5
# ============================================================

@app.get("/api/player-market")
async def player_market(
    fixture_id: Optional[int] = None,
    season: int = CURRENT_SEASON,
    recent_matches: int = 5,
):

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

            "message":
                (
                    "Configura APIFOOTBALL_KEY "
                    "en Render."
                ),
        }

    selected_fixture = None

    # --------------------------------------------------------
    # BUSCAR FIXTURE PRÓXIMO
    # --------------------------------------------------------

    if fixture_id is None:

        fixture_result = (
            await apifootball_get(
                "/fixtures",
                {
                    "date":
                        datetime.now(
                            timezone.utc,
                        ).strftime(
                            "%Y-%m-%d",
                        ),
                },
            )
        )

        if not fixture_result["ok"]:

            return {

                "status":
                    "error",

                "available":
                    False,

                "message":
                    fixture_result[
                        "error"
                    ],

                "details":
                    fixture_result[
                        "data"
                    ],
            }

        upcoming = []

        for fixture in (
            fixture_result[
                "data"
            ].get(
                "response",
                [],
            )
        ):

            status = (
                (
                    fixture.get(
                        "fixture",
                        {},
                    )
                    or {}
                )
                .get(
                    "status",
                    {},
                )
                or {}
            ).get(
                "short",
            )

            if status in {
                "NS",
                "TBD",
            }:

                upcoming.append(
                    fixture,
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

                "message":
                    (
                        "No hay un fixture "
                        "próximo disponible."
                    ),
            }

        selected_fixture = (
            upcoming[0]
        )

        fixture_id = (
            selected_fixture.get(
                "fixture",
                {},
            )
            .get(
                "id",
            )
        )

    else:

        fixture_result = (
            await apifootball_get(
                "/fixtures",
                {
                    "id":
                        fixture_id,
                },
            )
        )

        if (
            fixture_result["ok"]
            and fixture_result[
                "data"
            ].get(
                "response",
            )
        ):

            selected_fixture = (
                fixture_result[
                    "data"
                ][
                    "response"
                ][0]
            )

    if not selected_fixture:

        return {

            "status":
                "error",

            "available":
                False,

            "fixture_id":
                fixture_id,

            "message":
                (
                    "No se pudo obtener "
                    "el fixture."
                ),
        }

    teams = (
        selected_fixture.get(
            "teams",
            {},
        )
        or {}
    )

    league = (
        selected_fixture.get(
            "league",
            {},
        )
        or {}
    )

    fixture_info = (
        selected_fixture.get(
            "fixture",
            {},
        )
        or {}
    )

    home = (
        teams.get(
            "home",
            {},
        )
        or {}
    )

    away = (
        teams.get(
            "away",
            {},
        )
        or {}
    )

    # --------------------------------------------------------
    # ALINEACIONES
    # --------------------------------------------------------

    (
        _fixture_meta,
        lineup_players,
    ) = await get_confirmed_fixture_players(
        fixture_id,
    )

    if not lineup_players:

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

            "message":
                (
                    "Las alineaciones/estadísticas "
                    "de jugadores todavía "
                    "no están disponibles."
                ),
        }

    # --------------------------------------------------------
    # ESTADÍSTICAS DE TEMPORADA
    # Dos consultas: una por equipo.
    # --------------------------------------------------------

    home_stats = (
        await get_team_season_players(
            home.get("id"),
            season,
        )
        if home.get("id")
        else {}
    )

    away_stats = (
        await get_team_season_players(
            away.get("id"),
            season,
        )
        if away.get("id")
        else {}
    )

    season_map = {
        **home_stats,
        **away_stats,
    }

    # --------------------------------------------------------
    # ÚLTIMOS 5 PARTIDOS
    # Dos consultas de fixtures + dos consultas batch.
    # --------------------------------------------------------

    home_last_ids = (
        await get_team_last_fixture_ids(
            home.get("id"),
            recent_matches,
        )
        if home.get("id")
        else []
    )

    away_last_ids = (
        await get_team_last_fixture_ids(
            away.get("id"),
            recent_matches,
        )
        if away.get("id")
        else []
    )

    home_recent_data = (
        await get_fixtures_batch(
            home_last_ids,
        )
    )

    away_recent_data = (
        await get_fixtures_batch(
            away_last_ids,
        )
    )

    lineup_ids = {

        safe_int(
            p.get(
                "player_id",
            )
        )

        for p in lineup_players

        if p.get(
            "player_id",
        ) is not None
    }

    recent_home = (
        build_recent_form_from_fixture_data(
            home_recent_data,
            lineup_ids,
            recent_matches,
        )
    )

    recent_away = (
        build_recent_form_from_fixture_data(
            away_recent_data,
            lineup_ids,
            recent_matches,
        )
    )

    recent_map = {}

    for pid, rows in (
        recent_home.items()
    ):

        recent_map[pid] = (
            aggregate_recent_form(
                rows,
            )
        )

    for pid, rows in (
        recent_away.items()
    ):

        candidate = (
            aggregate_recent_form(
                rows,
            )
        )

        if (
            pid not in recent_map
            or candidate[
                "matches"
            ]
            > recent_map[
                pid
            ][
                "matches"
            ]
        ):

            recent_map[
                pid
            ] = candidate

    # --------------------------------------------------------
    # ENRIQUECER JUGADORES
    # --------------------------------------------------------

    enriched = []

    for player in lineup_players:

        pid = player.get(
            "player_id",
        )

        if pid is None:
            continue

        season_row = (
            season_map.get(
                pid,
                {},
            )
        )

        recent_row = (
            recent_map.get(
                pid,
                {},
            )
        )

        projection_rows = {}

        for market in (
            SUPPORTED_MARKETS
        ):

            projection_rows[
                market
            ] = (
                estimate_projection(
                    season_row,
                    recent_row,
                    starter=bool(
                        player.get(
                            "starter",
                        )
                    ),
                    market=market,
                )
            )

        enriched.append({

            **player,

            "season_stats":
                season_row,

            "recent_form":
                recent_row,

            "projections":
                projection_rows,

            "data_quality": {

                "lineup":
                    bool(
                        player.get(
                            "confirmed_lineup",
                        )
                    ),

                "season_stats":
                    bool(
                        season_row
                    ),

                "recent_form":
                    bool(
                        recent_row
                    ),
            },
        })

    starters = [
        p
        for p in enriched
        if p.get(
            "starter",
        )
    ]

    starters.sort(
        key=lambda p: (
            p.get(
                "season_stats",
                {},
            ).get(
                "minutes",
                0,
            ),

            p.get(
                "season_stats",
                {},
            ).get(
                "shots_per90",
                0,
            ),
        ),
        reverse=True,
    )

    return {

        "status":
            "real_player_data",

        "available":
            True,

        "fixture_id":
            fixture_id,

        "fixture": {

            "date":
                fixture_info.get(
                    "date",
                ),

            "status":
                fixture_info.get(
                    "status",
                    {},
                ),

            "league": {

                "id":
                    league.get(
                        "id",
                    ),

                "name":
                    league.get(
                        "name",
                    ),

                "country":
                    league.get(
                        "country",
                    ),
            },

            "home": {

                "id":
                    home.get(
                        "id",
                    ),

                "name":
                    home.get(
                        "name",
                    ),

                "logo":
                    home.get(
                        "logo",
                    ),
            },

            "away": {

                "id":
                    away.get(
                        "id",
                    ),

                "name":
                    away.get(
                        "name",
                    ),

                "logo":
                    away.get(
                        "logo",
                    ),
            },
        },

        "count":
            len(
                enriched,
            ),

        "starter_count":
            len(
                starters,
            ),

        "players":
            enriched,

        "markets":
            SUPPORTED_MARKETS,

        "senal":
            "DATOS REALES",

        "signal":
            "DATOS REALES",

        "motor":
            "Fútbol Analytics V1.5",

        "message":
            (
                "Alineaciones confirmadas + "
                "estadísticas de temporada + "
                "forma reciente disponibles."
            ),

        "api_usage_strategy":
            (
                "Estadísticas por equipo y "
                "fixtures recientes en lotes "
                "para reducir llamadas."
            ),
    }


# ============================================================
# PLAYER MARKETS - COMPATIBILIDAD FRONTEND
# ============================================================

@app.get("/api/player-markets")
async def player_markets(
    fixture_id: Optional[int] = None,
    season: int = CURRENT_SEASON,
    recent_matches: int = 5,
):

    result = await player_market(
        fixture_id=fixture_id,
        season=season,
        recent_matches=recent_matches,
    )

    if not result.get(
        "available",
        False,
    ):

        return {

            "status":
                result.get(
                    "status",
                    "waiting_data",
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
                    "Sin datos reales.",
                ),

            "motor":
                "Fútbol Analytics V1.5",
        }

    return {

        "status":
            "real_player_data",

        "available":
            True,

        "count":
            result.get(
                "count",
                0,
            ),

        "starter_count":
            result.get(
                "starter_count",
                0,
            ),

        "markets":
            result.get(
                "markets",
                SUPPORTED_MARKETS,
            ),

        "players":
            result.get(
                "players",
                [],
            ),

        "fixture":
            result.get(
                "fixture",
            ),

        "fixture_id":
            result.get(
                "fixture_id",
            ),

        "message":
            result.get(
                "message",
            ),

        "motor":
            "Fútbol Analytics V1.5",
    }


# ============================================================
# SCANNER MANUAL
# ============================================================

@app.post("/api/scanner")
def scanner(
    request: ScannerRequest,
):

    implied_probability = (
        probabilidad_implicita(
            request.odds,
        )
    )

    value_edge = (
        calcular_value_edge(
            request.probability_fa,
            request.odds,
        )
    )

    fa_rating = (
        calcular_fa_rating(
            request.probability_fa,
            value_edge,
            request.confidence,
        )
    )

    risk = (
        determinar_riesgo(
            fa_rating,
        )
    )

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            request.stake_percent,
            MAX_STAKE_PERCENT,
        ),
    )

    stake = calcular_stake(
        request.bankroll,
        stake_percent,
    )

    signal = (
        determinar_senal(
            value_edge,
        )
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
            value_edge > 0,

        "recommendation":
            (
                "OPORTUNIDAD CON VALOR"
                if value_edge > 0
                else "SIN VALOR POSITIVO"
            ),

        "motor":
            "Fútbol Analytics V1.5",
    }


# ============================================================
# SCANNER AUTOMÁTICO DESDE PROYECCIÓN
# ============================================================

@app.post(
    "/api/scanner-from-projection",
)
def scanner_from_projection(
    projection: float,
    line: float,
    odds: float,
    bankroll: float = 0,
    stake_percent: float = DEFAULT_STAKE_PERCENT,
    confidence: float = 70,
    player: str = "",
    market: str = "",
):

    if odds <= 1:

        return {

            "status":
                "error",

            "message":
                (
                    "La cuota debe ser "
                    "mayor que 1.00."
                ),
        }

    probability_fa = (
        poisson_probability_over(
            projection,
            line,
        )
    )

    value_edge = (
        calcular_value_edge(
            probability_fa,
            odds,
        )
    )

    rating = (
        calcular_fa_rating(
            probability_fa,
            value_edge,
            confidence,
        )
    )

    risk = (
        determinar_riesgo(
            rating,
        )
    )

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            stake_percent,
            MAX_STAKE_PERCENT,
        ),
    )

    return {

        "status":
            "ok",

        "player":
            player,

        "market":
            market,

        "projection":
            round(
                projection,
                3,
            ),

        "line":
            line,

        "odds":
            odds,

        "probability_fa":
            probability_fa,

        "implied_probability":
            probabilidad_implicita(
                odds,
            ),

        "value_edge":
            value_edge,

        "fa_rating":
            rating,

        "confidence":
            confidence,

        "risk":
            risk,

        "signal":
            determinar_senal(
                value_edge,
            ),

        "stake_percent":
            stake_percent,

        "recommended_stake":
            calcular_stake(
                bankroll,
                stake_percent,
            ),

        "projection_signal":
            market_signal_from_projection(
                projection,
                line,
            ),

        "recommendation":
            (
                "OPORTUNIDAD CON VALOR"
                if value_edge > 0
                else "SIN VALOR POSITIVO"
            ),

        "motor":
            "Fútbol Analytics V1.5",
    }


# ============================================================
# BANKROLL
# ============================================================

@app.post("/api/bankroll")
def bankroll_calculator(
    request: BankrollRequest,
):

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(
            request.stake_percent,
            MAX_STAKE_PERCENT,
        ),
    )

    stake = calcular_stake(
        request.bankroll,
        stake_percent,
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
                MIN_STAKE_PERCENT,
            ),

        "maximum_stake":
            calcular_stake(
                request.bankroll,
                MAX_STAKE_PERCENT,
            ),

        "all_in_allowed":
            False,
    }


# ============================================================
# BET TRACKER
# ============================================================

@app.post("/api/bets")
def create_bet(
    request: BetRequest,
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
                timezone.utc,
            ).isoformat(),
    }

    bets.append(
        bet,
    )

    return {

        "status":
            "created",

        "bet":
            bet,
    }


@app.get("/api/bets")
def get_bets():

    return {

        "count":
            len(bets),

        "bets":
            bets,
    }


# ============================================================
# KPIs
# ============================================================

@app.get("/api/kpis")
def get_kpis():

    total_bets = len(
        bets,
    )

    settled_bets = [
        bet
        for bet in bets
        if bet["result"]
        in {
            "GANADA",
            "PERDIDA",
            "ANULADA",
        }
    ]

    total_staked = sum(
        safe_number(
            bet["stake"],
        )
        for bet in settled_bets
    )

    profit = 0.0

    wins = 0

    losses = 0

    voids = 0

    for bet in settled_bets:

        stake = safe_number(
            bet["stake"],
        )

        odds = safe_number(
            bet["odds"],
        )

        result = bet["result"]

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
        profit
        / total_staked
        * 100
        if total_staked > 0
        else 0
    )

    win_rate = (
        wins
        / (wins + losses)
        * 100
        if wins + losses > 0
        else 0
    )

    return {

        "total_bets":
            total_bets,

        "settled_bets":
            len(
                settled_bets,
            ),

        "profit":
            round(
                profit,
                2,
            ),

        "yield":
            round(
                yield_percent,
                2,
            ),

        "win_rate":
            round(
                win_rate,
                2,
            ),

        "wins":
            wins,

        "losses":
            losses,

        "voids":
            voids,
    }
