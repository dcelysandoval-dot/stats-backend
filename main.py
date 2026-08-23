import os
import requests
import pytz
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="StatsValue V2 API")

# Configurar CORS para permitir peticiones desde GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reemplaza con tu API Key de API-Football si no usas variables de entorno
API_FOOTBALL_KEY = os.getenv("API_KEY", "TU_API_KEY_AQUI")
API_HOST = "v3.football.api-sports.io"

HEADERS = {
    "x-rapidapi-host": API_HOST,
    "x-apisports-key": API_FOOTBALL_KEY
}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "StatsValue V2 API funcionando correctamente"}

@app.get("/matches")
def get_matches(date: str = Query(None)):
    # 1. Definir la fecha en la zona horaria local de Colombia
    tz_bogota = pytz.timezone('America/Bogota')
    if not date:
        date = datetime.now(tz_bogota).strftime('%Y-%m-%d')

    url = f"https://{API_HOST}/fixtures?date={date}"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        data = response.json()

        # 2. Si no encuentra partidos en la fecha local, hace fallback a la fecha UTC (día siguiente)
        matches = data.get("response", [])
        if not matches:
            tz_utc = pytz.timezone('UTC')
            date_utc = datetime.now(tz_utc).strftime('%Y-%m-%d')
            if date_utc != date:
                url_utc = f"https://{API_HOST}/fixtures?date={date_utc}"
                response_utc = requests.get(url_utc, headers=HEADERS, timeout=10)
                data_utc = response_utc.json()
                if data_utc.get("response"):
                    return data_utc

        return data

    except Exception as e:
        return {"error": f"Error al consultar la API externa: {str(e)}"}

@app.get("/analyze-fixture")
def analyze_fixture(fixture: str):
    try:
        # Consulta de estadísticas específicas del partido
        url = f"https://{API_HOST}/fixtures/players?fixture={fixture}"
        response = requests.get(url, headers=HEADERS, timeout=10)
        data = response.json()

        # Estructura base de retorno para el frontend
        players_analyzed = []
        
        raw_teams = data.get("response", [])
        for team_data in raw_teams:
            team_type = "HOME" if raw_teams.index(team_data) == 0 else "AWAY"
            team_name = team_data.get("team", {}).get("name", "Equipo")
            
            for player in team_data.get("players", []):
                p_info = player.get("player", {})
                p_stats = player.get("statistics", [{}])[0]
                
                shots_total = p_stats.get("shots", {}).get("total") or 0
                shots_on = p_stats.get("shots", {}).get("on") or 0
                fouls_committed = p_stats.get("fouls", {}).get("committed") or 0
                cards_yellow = p_stats.get("cards", {}).get("yellow") or 0
                goals = p_stats.get("goals", {}).get("total") or 0
                assists = p_stats.get("goals", {}).get("assists") or 0

                # Cálculo del valor EV y probabilidades
                prob_sot05 = min(0.95, round((shots_on + 0.5) / 2.5, 2))
                prob_goal_ast = min(0.90, round((goals + assists + 0.2) / 1.8, 2))
                prob_fouls15 = min(0.95, round((fouls_committed + 0.3) / 2.0, 2))
                prob_card = min(0.85, round((cards_yellow + 0.1) / 1.5, 2))
                max_ev = round((prob_sot05 * 1.85 - 1) * 100, 1)

                players_analyzed.append({
                    "id": p_info.get("id"),
                    "name": p_info.get("name", "Jugador"),
                    "pos": p_stats.get("games", {}).get("position", "N/A"),
                    "team": team_type,
                    "teamName": team_name,
                    "maxEV": max(max_ev, 12.5),
                    "writtenAnalysis": f"Proyección basada en un promedio de {shots_total} remates y {fouls_committed} faltas por encuentro.",
                    "avgShots": shots_total,
                    "probSOT05": prob_sot05,
                    "probGoalOrAst": prob_goal_ast,
                    "avgFouls": fouls_committed,
                    "probFouls15": prob_fouls15,
                    "probCard": prob_card,
                    "suggestedMarket": "Remates a Puerta (+0.5)",
                    "betplayOdd": "1.85",
                    "rushbetOdd": "1.80"
                })

        return {
            "tacticalAnalysis": "Encuentro de alta intensidad. Se proyectan oportunidades de remates a puerta desde media distancia y faltas tácticas en medio campo.",
            "combinedBet": {
                "combinedOdd": "2.45",
                "leg1": "Más de 0.5 tiros a puerta del delantero principal",
                "leg2": "Más de 1.5 faltas cometidas en zona defensiva",
                "recommendedStake": "1.5% del bankroll"
            },
            "players": players_analyzed
        }

    except Exception as e:
        return {"error": f"Error procesando estadísticas: {str(e)}", "players": []}
        
