import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# FÚTBOL ANALYTICS - BACKEND V1.1
# ============================================================

app = FastAPI(
    title="Fútbol Analytics API",
    version="1.1.0",
    description="Motor inicial de análisis estadístico, Value Edge y gestión de bankroll."
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
    Mantiene el IVJ original del proyecto.

    Esta versión inicial utiliza el marcador del partido.
    Posteriormente lo reemplazaremos por variables reales
    de jugadores y mercados estadísticos.
    """

    score = match.get("score", {}).get("fullTime", {})

    home_score = score.get("home", 0) or 0
    away_score = score.get("away", 0) or 0

    ivj_base = 5.0 + (home_score + away_score) * 0.5

    return round(ivj_base, 2)


def probabilidad_implicita(cuota: float) -> float:
    """
    Convierte una cuota decimal en probabilidad implícita.

    Ejemplo:
    cuota 2.00 = 50%
    cuota 1.50 = 66.67%
    """

    if cuota <= 1:
        return 0.0

    return round((1 / cuota) * 100, 2)


def calcular_value_edge(
    probabilidad_fa: float,
    cuota: float
) -> float:
    """
    Value Edge = Probabilidad estimada por Fútbol Analytics
                  - Probabilidad implícita de la cuota.
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
    FA Rating inicial de 0 a 100.

    No representa una garantía de resultado.
    Es un índice interno de calidad de oportunidad.
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
    Calcula el dinero recomendado según bankroll.

    El sistema limita automáticamente el stake
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
        "version": "1.1.0",
        "message": "Fútbol Analytics API corriendo"
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "healthy",
        "service": "futbol-analytics-backend"
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
            timeout=10.0
        ) as client:

            response = await client.get(
                url,
                headers=headers
            )

            if response.status_code != 200:

                return {
                    "matches": [],
                    "error": (
                        f"Error en API externa "
                        f"({response.status_code})"
                    ),
                    "details": (
                        response.json()
                        if response.content
                        else "Sin detalle"
                    )
                }

            data = response.json()

            raw_matches = data.get(
                "matches",
                []
            )

            processed_matches = []

            for m in raw_matches:

                ivj_val = calcular_indice_ivj(m)

                processed_matches.append({

                    "id": m.get("id"),

                    "utcDate": m.get(
                        "utcDate"
                    ),

                    "status": m.get(
                        "status"
                    ),

                    "competition": {

                        "name": m.get(
                            "competition",
                            {}
                        ).get("name"),

                        "emblem": m.get(
                            "competition",
                            {}
                        ).get("emblem")
                    },

                    "homeTeam": {

                        "name": m.get(
                            "homeTeam",
                            {}
                        ).get("name"),

                        "crest": m.get(
                            "homeTeam",
                            {}
                        ).get("crest")
                    },

                    "awayTeam": {

                        "name": m.get(
                            "awayTeam",
                            {}
                        ).get("name"),

                        "crest": m.get(
                            "awayTeam",
                            {}
                        ).get("crest")
                    },

                    "score": m.get(
                        "score",
                        {}
                    ).get("fullTime"),

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

            "matches": [],

            "error": (
                "Error de conexión "
                "con el proveedor de datos"
            ),

            "details": str(exc)
        }


# ============================================================
# COMPATIBILIDAD CON EL FRONTEND ACTUAL
# ============================================================

@app.get("/api/partidos-hoy")
async def partidos_hoy():

    return await get_matches(days_ahead=1)


# ============================================================
# PLAYER MARKET SCANNER
# ============================================================

@app.post("/api/scanner")
def scanner(request: ScannerRequest):

    implied_probability = probabilidad_implicita(
        request.odds
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
        )
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

        "losses": losses
    }
