import os
from datetime import datetime, timedelta, timezone
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Fútbol Analytics - Backend IVJ")

# Configuración de CORS para conexión con GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clave de API obtenida desde variable de entorno (Render) o valor por defecto
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "TU_API_KEY_AQUI")
BASE_URL = "https://api.football-data.org/v4"

# Ligas disponibles en la cuenta gratuita de Football-Data.org
FREE_LEAGUES = "PL,PD,SA,BL1,FL1,CL,EC,WC"

def calcular_indice_ivj(match: dict) -> float:
    """
    Función para calcular el Índice de Valor del Jugador / Partido (IVJ).
    Ajusta esta lógica con tus parámetros estadísticos reales.
    """
    score = match.get("score", {}).get("fullTime", {})
    home_score = score.get("home", 0) or 0
    away_score = score.get("away", 0) or 0
    
    # Ejemplo básico de ponderación IVJ
    ivj_base = 5.0 + (home_score + away_score) * 0.5
    return round(ivj_base, 2)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Fútbol Analytics API corriendo"}

@app.get("/api/matches")
async def get_matches(days_ahead: int = 1):
    """
    Obtiene los partidos programados y en vivo.
    Si no hay partidos hoy, expande la búsqueda automáticamente.
    """
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    
    # Manejo de rango de fechas en UTC para evitar desfases por zona horaria local
    now_utc = datetime.now(timezone.utc)
    date_from = now_utc.strftime("%Y-%m-%d")
    date_to = (now_utc + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    url = f"{BASE_URL}/matches?dateFrom={date_from}&dateTo={date_to}&competitions={FREE_LEAGUES}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return {
                    "matches": [],
                    "error": f"Error en API externa ({response.status_code})",
                    "details": response.json() if response.content else "Sin detalle"
                }

            data = response.json()
            raw_matches = data.get("matches", [])

            processed_matches = []
            for m in raw_matches:
                ivj_val = calcular_indice_ivj(m)
                
                processed_matches.append({
                    "id": m.get("id"),
                    "utcDate": m.get("utcDate"),
                    "status": m.get("status"),
                    "competition": {
                        "name": m.get("competition", {}).get("name"),
                        "emblem": m.get("competition", {}).get("emblem")
                    },
                    "homeTeam": {
                        "name": m.get("homeTeam", {}).get("name"),
                        "crest": m.get("homeTeam", {}).get("crest")
                    },
                    "awayTeam": {
                        "name": m.get("awayTeam", {}).get("name"),
                        "crest": m.get("awayTeam", {}).get("crest")
                    },
                    "score": m.get("score", {}).get("fullTime"),
                    "ivjIndex": ivj_val
                })

            return {
                "count": len(processed_matches),
                "dateFrom": date_from,
                "dateTo": date_to,
                "matches": processed_matches
            }

    except httpx.RequestError as exc:
        return {
            "matches": [],
            "error": "Error de conexión con el proveedor de datos de fútbol",
            "details": str(exc)
        }
        
