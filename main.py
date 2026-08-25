import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="Football Stats Analytics API")

# Habilitar CORS para consumo desde GitHub Pages u otros dominios
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY")
BASE_URL = "https://api.football-data.org/v4"

def get_headers():
    if not FOOTBALL_DATA_KEY:
        raise HTTPException(
            status_code=500, 
            detail="La clave FOOTBALL_DATA_KEY no está configurada en las variables de entorno."
        )
    return {"X-Auth-Token": FOOTBALL_DATA_KEY}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "API de Análisis Estadístico de Fútbol activa"}

@app.get("/api/matches/{competition_code}")
async def get_matches(competition_code: str):
    """
    Obtiene los partidos próximos de una competición (ej. PL, CL, SA, PD)
    """
    url = f"{BASE_URL}/competitions/{competition_code}/matches?status=SCHEDULED"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=get_headers(), timeout=10.0)
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.json())
            
            data = response.json()
            matches = data.get("matches", [])
            
            # Procesamiento básico para devolver estructura limpia
            processed_matches = []
            for m in matches:
                processed_matches.append({
                    "id": m.get("id"),
                    "utcDate": m.get("utcDate"),
                    "homeTeam": m.get("homeTeam", {}).get("name"),
                    "awayTeam": m.get("awayTeam", {}).get("name"),
                    "matchday": m.get("matchday")
                })
                
            return {"count": len(processed_matches), "matches": processed_matches}
            
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Error al conectar con la API externa: {exc}")
            
