import os
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI(
    title="Fútbol Analytics API",
    description="Backend para análisis estadístico de partidos y mercados de jugadores",
    version="2.0.0"
)

# Configuración de CORS para permitir peticiones desde GitHub Pages o local
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Puedes especificar ["https://tu-usuario.github.io"] si deseas restringirlo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clave y URL base de Football-Data.org
FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY", "")
BASE_URL = "https://api.football-data.org/v4"

HEADERS = {
    "X-Auth-Token": FOOTBALL_DATA_KEY
}

@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "Fútbol Analytics API está funcionando correctamente",
        "provider": "Football-Data.org"
    }

@app.get("/api/partidos-hoy")
async def obtener_partidos_hoy():
    """
    Obtiene todos los partidos programados para la fecha actual.
    """
    url = f"{BASE_URL}/matches"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=HEADERS, timeout=10.0)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Error al conectar con la API externa: {exc}")
            
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code, 
            detail=f"Error devuelto por Football-Data.org: {response.text}"
        )
        
    return response.json()

@app.get("/api/champions")
async def obtener_partidos_champions():
    """
    Obtiene los partidos y cuadro de la UEFA Champions League (código 'CL').
    """
    url = f"{BASE_URL}/competitions/CL/matches"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=HEADERS, timeout=10.0)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Error de red: {exc}")
            
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code, 
            detail="No se pudo obtener la información de la Champions League"
        )
        
    return response.json()

@app.get("/api/posiciones/{codigo_liga}")
async def obtener_tabla_posiciones(codigo_liga: str):
    """
    Obtiene la tabla de posiciones de una competición.
    Ejemplos de códigos: CL (Champions), PL (Premier League), PD (LaLiga), SA (Serie A).
    """
    url = f"{BASE_URL}/competitions/{codigo_liga.upper()}/standings"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=HEADERS, timeout=10.0)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Error de red: {exc}")
            
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code, 
            detail=f"No se pudo consultar las posiciones para la liga '{codigo_liga}'"
        )
        
    return response.json()

@app.get("/api/partido/{partido_id}")
async def obtener_detalle_partido(partido_id: int):
    """
    Obtiene el detalle específico de un partido por su ID.
    """
    url = f"{BASE_URL}/matches/{partido_id}"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=HEADERS, timeout=10.0)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Error de red: {exc}")
            
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code, 
            detail="Partido no encontrado o error en la petición"
        )
        
    return response.json()
            
