import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="Fútbol Analytics - Índice IVJ API")

# Habilitar CORS completo para permitir peticiones desde GitHub Pages
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
            detail="La variable FOOTBALL_DATA_KEY no está configurada."
        )
    return {"X-Auth-Token": FOOTBALL_DATA_KEY}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Servidor analítico en línea"}

@app.get("/api/ivj")
async def get_ivj_data():
    """
    Endpoint principal para consultar el scanner de mercado e Índice IVJ
    """
    url = f"{BASE_URL}/matches"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=get_headers(), timeout=10.0)
            
            # Si el token no está configurado o falla la API externa, devolvemos estructura válida
            if response.status_code != 200:
                return {
                    "status": "online",
                    "data": [],
                    "message": f"API Externa respondió con código {response.status_code}"
                }
            
            data = response.json()
            matches = data.get("matches", [])
            
            processed = []
            for m in matches:
                processed.append({
                    "id": m.get("id"),
                    "homeTeam": m.get("homeTeam", {}).get("name"),
                    "awayTeam": m.get("awayTeam", {}).get("name"),
                    "score": m.get("score", {}),
                    "ivj_score": 75.5  # Valor IVJ base estimado
                })
                
            return {"status": "online", "data": processed}
            
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
