import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# FÚTBOL ANALYTICS - BACKEND V1.3
# ============================================================

app = FastAPI(
    title="Fútbol Analytics API",
    version="1.3.0",
    description=(
        "Motor de análisis estadístico, Player Market Scanner, "
        "Value Edge y gestión de bankroll."
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
    "TU_API_KEY_AQUI"
)

BASE_URL = "https://api.football-data.org/v4"

FREE_LEAGUES = "PL,PD,SA,BL1,FL1,CL,EC,WC"


# ============================================================
# CONFIGURACIÓN BANKROLL
# ============================================================

MIN_STAKE_PERCENT = 1.0
DEFAULT_STAKE_PERCENT = 2.0
MAX_STAKE_PERCENT = 3.0


# ============================================================
# FUNCIONES DEL MOTOR ANALÍTICO
# ============================================================

def calcular_indice_ivj(match: dict) -> float:
    """
    IVJ inicial del proyecto.

    Actualmente utiliza los goles del partido.
    En futuras versiones podrá incorporar:
    - volumen ofensivo
    - tiros
    - tiros a puerta
    - posesión
    - xG
    - ritmo
    - tarjetas
    - faltas
    - rendimiento de jugadores
    """

    score = match.get("score", {}).get("fullTime", {})

    home_score = score.get("home", 0) or 0
    away_score = score.get("away", 0) or 0

    ivj_base = 5.0 + (home_score + away_score) * 0.5

    return round(ivj_base, 2)


def probabilidad_implicita(cuota: float) -> float:
    """
    Convierte cuota decimal en probabilidad implícita.

    2.00 = 50%
    1.50 = 66.67%
    """

    if cuota <= 1:
        return 0.0

    return round((1 / cuota) * 100, 2)


def calcular_value_edge(
    probabilidad_fa: float,
    cuota: float
) -> float:
    """
    Value Edge =
    Probabilidad FA - Probabilidad implícita de cuota.
    """

    probabilidad_cuota = probabilidad_implicita(cuota)

    return round(
        probabilidad_fa - probabilidad_cuota,
        2
    )


def calcular_fa_rating(
    probabilidad_fa: float,
    value_edge: float,
    confianza: float = 70
) -> int:
    """
    FA Rating interno de 0 a 100.
    """

    score = (
        probabilidad_fa * 0.45
        + max(value_edge, 0) * 1.5
        + confianza * 0.25
    )

    score = max(0, min(score, 100))

    return round(score)


def calcular_stake(
    bankroll: float,
    stake_percent: float
) -> float:
    """
    Calcula stake recomendado.

    El sistema limita automáticamente
    entre 1% y 3%.
    """

    stake_percent = max(
        MIN_STAKE_PERCENT,
        min(stake_percent, MAX_STAKE_PERCENT)
    )

    return round(
        bankroll * (stake_percent / 100),
        2
    )


def determinar_riesgo(fa_rating: int) -> str:

    if fa_rating >= 80:
        return "BAJO"

    if fa_rating >= 70:
        return "MEDIO"

    if fa_rating >= 60:
        return "MODERADO"

    return "ALTO"


def determinar_senal(value_edge: float) -> str:
    """
    Clasificación interna del motor.
    """

    if value_edge >= 15:
        return "OPORTUNIDAD ALTA"

    if value_edge >= 8:
        return "OPORTUNIDAD MEDIA"

    if value_edge > 0:
        return "VALOR BAJO"

    return "SIN VALOR"


# ============================================================
# MODELOS
# ============================================================

class BankrollRequest(BaseModel):

    bankroll: float

    stake_percent: float = DEFAULT_STAKE_PERCENT


class ScannerRequest(BaseModel):

    player: str

    market: str

    line: float

    odds: float

    probability_fa: float

    bankroll: float = 0

    stake_percent: float = DEFAULT_STAKE_PERCENT

    confidence: float = 70


class BetRequest(BaseModel):

    event: str

    market: str

    odds: float

    stake: float

    result: str = "PENDIENTE"


# ============================================================
# MEMORIA TEMPORAL DE APUESTAS
# ============================================================

bets = []


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def read_root():

    return {
        "status": "ok",
        "project": "Fútbol Analytics",
        "version": "1.3.0",
        "engine": "Player Market Scanner V1.3",
        "message": "Fútbol Analytics API corriendo"
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "healthy",
        "service": "futbol-analytics-backend",
        "version": "1.3.0"
    }


# ============================================================
# PARTIDOS
# ============================================================

@app.get("/api/matches")
async def get_matches(days_ahead: int = 1):

    headers = {
        "X-Auth-Token": FOOTBALL_DATA_API_KEY
    }

    now_utc = datetime.now(timezone.utc)

    date_from = now_utc.strftime("%Y-%m-%d")

    date_to = (
        now_utc + timedelta(days=days_ahead)
    ).strftime("%Y-%m-%d")

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
                        f"Error en API externa "
                        f"({response.status_code})"
                    ),
                    "details": details
                }

            data = response.json()

            raw_matches = data.get(
                "matches",
                []
            )

            processed_matches = []

            for match in raw_matches:

                ivj_val = calcular_indice_ivj(
                    match
                )

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

                full_time = match.get(
                    "score",
                    {}
                ).get(
                    "fullTime",
                    {}
                )

                processed_matches.append({

                    "id": match.get("id"),

                    "utcDate": match.get(
                        "utcDate"
                    ),

                    "status": match.get(
                        "status"
                    ),

                    "competition": {

                        "name": competition.get(
                            "name"
                        ),

                        "emblem": competition.get(
                            "emblem"
                        )
                    },

                    "homeTeam": {

                        "name": home_team.get(
                            "name"
                        ),

                        "crest": home_team.get(
                            "crest"
                        )
                    },

                    "awayTeam": {

                        "name": away_team.get(
                            "name"
                        ),

                        "crest": away_team.get(
                            "crest"
                        )
                    },

                    "score": full_time,

                    "ivjIndex": ivj_val
                })

            return {

                "count": len(
                    processed_matches
                ),

                "dateFrom": date_from,

                "dateTo": date_to,

                "matches": processed_matches
            }

    except httpx.RequestError as exc:

        return {

            "count": 0,

            "matches": [],

            "error": (
                "Error de conexión "
                "con el proveedor de datos"
            ),

            "details": str(exc)
        }

    except Exception as exc:

        return {

            "count": 0,

            "matches": [],

            "error": (
                "Error interno procesando "
                "los partidos"
            ),

            "details": str(exc)
        }


# ============================================================
# COMPATIBILIDAD CON FRONTEND
# ============================================================

@app.get("/api/partidos-hoy")
async def partidos_hoy():

    return await get_matches(
        days_ahead=1
    )


# ============================================================
# PLAYER MARKET SCANNER
# ============================================================

@app.post("/api/scanner")
def scanner(request: ScannerRequest):

    implied_probability = (
        probabilidad_implicita(
            request.odds
        )
    )

    value_edge = calcular_value_edge(
        request.probability_fa,
        request.odds
    )

    fa_rating = calcular_fa_rating(
        request.probability_fa,
        value_edge,
        request.confidence
    )

    risk = determinar_riesgo(
        fa_rating
    )

    stake = calcular_stake(
        request.bankroll,
        request.stake_percent
    )

    value_positive = value_edge > 0

    signal = determinar_senal(
        value_edge
    )

    return {

        "player": request.player,

        "market": request.market,

        "line": request.line,

        "odds": request.odds,

        "probability_fa": request.probability_fa,

        "implied_probability": implied_probability,

        "value_edge": value_edge,

        "fa_rating": fa_rating,

        "confidence": request.confidence,

        "risk": risk,

        "signal": signal,

        "stake_percent": min(
            max(
                request.stake_percent,
                MIN_STAKE_PERCENT
            ),
            MAX_STAKE_PERCENT
        ),

        "recommended_stake": stake,

        "value_positive": value_positive,

        "recommendation": (
            "OPORTUNIDAD CON VALOR"
            if value_positive
            else "SIN VALOR POSITIVO"
        ),

        "motor": "Fútbol Analytics V1.3"
    }


# ============================================================
# PLAYER MARKET - ESTADO DEL MOTOR
# ============================================================

@app.get("/api/player-market")
def player_market():

    """
    Endpoint compatible con el frontend actual.

    IMPORTANTE:
    No inventa estadísticas.

    Mientras no exista una fuente real de
    estadísticas de jugadores, devuelve
    estado SIN DATOS REALES.
    """

    return {

        "status": "waiting_data",

        "available": False,

        "jugador": "Sin datos reales",

        "player": "Sin datos reales",

        "mercado": "Remates",

        "market": "Remates",

        "linea": None,

        "line": None,

        "fa_rating": None,

        "faRating": None,

        "probabilidad": None,

        "probability_fa": None,

        "cuota": None,

        "odds": None,

        "probabilidad_implicita": None,

        "value_edge": None,

        "valueEdge": None,

        "senal": "SIN DATOS",

        "signal": "SIN DATOS",

        "motor": "Fútbol Analytics V1.3",

        "tipo": "real_pending",

        "message": (
            "El motor está preparado para "
            "recibir estadísticas reales de jugadores."
        )
    }


# ============================================================
# PLAYER MARKETS - MULTIPLES OPORTUNIDADES
# ============================================================

@app.get("/api/player-markets")
def player_markets():

    """
    Endpoint futuro para múltiples mercados.

    Ejemplo futuro:

    [
        {
            jugador: "...",
            mercado: "Remates",
            linea: 2.5,
            probabilidad: 64,
            cuota: 1.90,
            value_edge: 11.37,
            fa_rating: 78
        }
    ]

    Por ahora NO se generan picks ficticios.
    """

    return {

        "status": "waiting_data",

        "available": False,

        "count": 0,

        "markets": [],

        "message": (
            "No existen todavía mercados de "
            "jugadores con estadísticas reales."
        ),

        "motor": "Fútbol Analytics V1.3"
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

        "bankroll": request.bankroll,

        "stake_percent": stake_percent,

        "recommended_stake": stake,

        "minimum_stake": calcular_stake(
            request.bankroll,
            MIN_STAKE_PERCENT
        ),

        "maximum_stake": calcular_stake(
            request.bankroll,
            MAX_STAKE_PERCENT
        ),

        "all_in_allowed": False
    }


# ============================================================
# BET TRACKER
# ============================================================

@app.post("/api/bets")
def create_bet(
    request: BetRequest
):

    bet = {

        "id": len(bets) + 1,

        "event": request.event,

        "market": request.market,

        "odds": request.odds,

        "stake": request.stake,

        "result": request.result,

        "created_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    bets.append(bet)

    return {

        "status": "created",

        "bet": bet
    }


@app.get("/api/bets")
def get_bets():

    return {

        "count": len(bets),

        "bets": bets
    }


# ============================================================
# KPIs
# ============================================================

@app.get("/api/kpis")
def get_kpis():

    total_bets = len(bets)

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

        stake = bet["stake"]

        odds = bet["odds"]

        result = bet["result"]

        if result == "GANADA":

            profit += stake * (
                odds - 1
            )

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

        "settled_bets": len(
            settled_bets
        ),

        "profit": round(
            profit,
            2
        ),

        "yield": round(
            yield_percent,
            2
        ),

        "win_rate": round(
            win_rate,
            2
        ),

        "wins": wins,

        "losses": losses,

        "voids": voids
    }
