import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="Fútbol Analytics API")

# Permitir solicitudes desde tu frontend en GitHub Pages
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
        # Retorna diccionario vacío si aún no configuras la key para evitar crash instantáneo
        return {}
    return {"X-Auth-Token": FOOTBALL_DATA_KEY}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Backend analítico activo"}

# ENDPOINT REQUERIDO POR TU FRONTEND
@app.get("/api/partidos-hoy")
async def get_partidos_hoy():
    """
    Ruta que consulta tu frontend para mostrar las métricas del día.
    """
    url = f"{BASE_URL}/matches"
    headers = get_headers()
    
    if not headers:
        return {"status": "error", "message": "Falta la variable FOOTBALL_DATA_KEY en Render", "partidos": []}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                return {
                    "status": "warning", 
                    "message": f"API externa devolvió status {response.status_code}",
                    "partidos": []
                }
            
            data = response.json()
            matches = data.get("matches", [])
            
            # Formateo de respuesta esperada por tu app
            partidos_procesados = []
            for m in matches:
                partidos_procesados.append({
                    "id": m.get("id"),
                    "local": m.get("homeTeam", {}).get("name"),
                    "visitante": m.get("awayTeam", {}).get("name"),
                    "liga": m.get("competition", {}).get("name"),
                    "utcDate": m.get("utcDate"),
                    "ivj": 78.4  # Cálculo/Métrica base
                })
                
            return {"status": "success", "partidos": partidos_procesados}
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
