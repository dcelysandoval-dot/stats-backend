import math
import os
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cache_store = {}
CACHE_DURATION = 14400  # 4 horas


def poisson(k, lambd):
  if lambd <= 0:
    return 0
  return (math.pow(lambd, k) * math.exp(-lambd)) / math.factorial(k)


def prob_at_least(k, lambd):
  total = sum(poisson(i, lambd) for i in range(k))
  return max(0.01, min(0.99, 1 - total))


def analyze_tactics(formation_home: str, formation_away: str):
  """Analiza cómo chocan dos esquemas tácticos para proyectar la dinámica del partido."""
  f_h = formation_home if formation_home else "4-3-3"
  f_a = formation_away if formation_away else "4-4-2"

  tactical_notes = []
  home_wing_boost = 1.0
  away_wing_boost = 1.0

  if "5-" in f_h or "3-" in f_h:
    tactical_notes.append(
        f"El equipo local ({f_h}) usa carrileros de proyección alta. Genera"
        " espacio en bandas para centros y remates lejanos."
    )
    home_wing_boost = 1.25
  else:
    tactical_notes.append(
        f"El equipo local ({f_h}) propone bloque posicional estándar con juego"
        " por el carril central."
    )

  if "4-2-3-1" in f_a or "4-3-3" in f_a:
    tactical_notes.append(
        f"El visitante ({f_a}) explota transiciones rápidas con extremos"
        " abiertos, aumentando la probabilidad de faltas/tarjetas en los"
        " laterales rivales."
    )
    away_wing_boost = 1.20

  summary = (
      " ".join(tactical_notes)
      + " Se proyecta un juego con alta densidad en el medio campo y balones a"
      " las espaldas de la zaga."
  )
  return summary, home_wing_boost, away_wing_boost


@app.get("/")
def home():
  return {"status": "Backend V2 Avanzado + Análisis Táctico Activo"}


@app.get("/matches")
def get_matches(date: str):
  api_key = os.environ.get("API_SPORTS_KEY")
  if not api_key:
    return {
        "error": "API Key no configurada en las variables de entorno de Render"
    }

  headers = {"x-apisports-key": api_key}
  url = f"https://v3.football.api-sports.io/fixtures?date={date}"

  try:
    response = requests.get(url, headers=headers, timeout=10)
    return response.json()
  except Exception as e:
    return {"error": f"Error consultando partidos: {str(e)}"}


@app.get("/analyze-fixture")
def analyze_fixture(fixture: str):
  now = time.time()
  if fixture in cache_store:
    cached_data, timestamp = cache_store[fixture]
    if now - timestamp < CACHE_DURATION:
      return cached_data

  api_key = os.environ.get("API_SPORTS_KEY")
  if not api_key:
    return {"players": [], "error": "API Key no configurada"}

  headers = {"x-apisports-key": api_key}
  url = f"https://v3.football.api-sports.io/fixtures/lineups?fixture={fixture}"

  try:
    response = requests.get(url, headers=headers, timeout=10)
    data = response.json()
  except Exception as e:
    return {"players": [], "error": f"Error en petición: {str(e)}"}

  players_result = []
  tactical_analysis = "Alineaciones no confirmadas aún. Datos basados en medias promedio."

  if (
      "response" in data
      and isinstance(data["response"], list)
      and len(data["response"]) >= 2
  ):
    team_home = data["response"][0]
    team_away = data["response"][1]

    form_h = team_home.get("formation", "4-3-3")
    form_a = team_away.get("formation", "4-4-2")

    tactical_analysis, boost_h, boost_a = analyze_tactics(form_h, form_a)

    for team_index, team_data in enumerate(data["response"]):
      is_home = team_index == 0
      boost = boost_h if is_home else boost_a
      start_xi = team_data.get("startXI", [])

      for item in start_xi:
        player_obj = item.get("player", {})
        pos = player_obj.get("pos", "M")
        name = player_obj.get("name", "Jugador")

        # Promedios Estadísticos Avanzados (Poisson Lambdas)
        if pos == "F":
          l_shots, l_sot, l_goal, l_ast, l_fouls, l_cards = (
              2.6 * boost,
              1.35 * boost,
              0.48,
              0.25,
              1.2,
              0.14,
          )
        elif pos == "M":
          l_shots, l_sot, l_goal, l_ast, l_fouls, l_cards = (
              1.4,
              0.65,
              0.19,
              0.32,
              1.5,
              0.22,
          )
        elif pos == "D":
          l_shots, l_sot, l_goal, l_ast, l_fouls, l_cards = (
              0.5,
              0.18,
              0.06,
              0.12,
              1.7 * boost,
              0.28,
          )
        else:
          l_shots, l_sot, l_goal, l_ast, l_fouls, l_cards = (
              0.0,
              0.0,
              0.0,
              0.0,
              0.2,
              0.05,
          )

        # Probabilidades Calculadas
        prob_shots15 = prob_at_least(2, l_shots)
        prob_sot05 = prob_at_least(1, l_sot)
        prob_goal = prob_at_least(1, l_goal)
        prob_ast = prob_at_least(1, l_ast)
        prob_goal_or_ast = min(0.95, prob_goal + prob_ast * 0.7)
        prob_fouls15 = prob_at_least(2, l_fouls)
        prob_card = prob_at_least(1, l_cards)

        # Selección de Mercado Sugerido
        market_label = "+0.5 Remates a Puerta"
        chosen_prob = prob_sot05

        if pos == "F":
          if prob_goal_or_ast >= 0.55:
            market_label = "Marca o Asiste"
            chosen_prob = prob_goal_or_ast
          elif prob_shots15 >= 0.65:
            market_label = "+1.5 Disparos Totales"
            chosen_prob = prob_shots15
        elif pos == "D" and prob_fouls15 >= 0.50:
          market_label = "+1.5 Faltas Cometidas"
          chosen_prob = prob_fouls15

        # Cotizaciones Simuladas (Betplay / Rushbet)
        betplay_odd = (
            round((1 / chosen_prob) * 1.07, 2) if chosen_prob > 0 else 1.01
        )
        rushbet_odd = (
            round((1 / chosen_prob) * 1.05, 2) if chosen_prob > 0 else 1.01
        )

        ev_betplay = ((chosen_prob * betplay_odd) - 1) * 100
        ev_rushbet = ((chosen_prob * rushbet_odd) - 1) * 100

        best_bookie = "Betplay" if betplay_odd >= rushbet_odd else "Rushbet"
        best_odd = max(betplay_odd, rushbet_odd)
        max_ev = max(ev_betplay, ev_rushbet)

        # Análisis Escrito Individual
        player_written_analysis = (
            f"Presenta una proyección de {l_shots:.1f} disparos totales y"
            f" {l_fouls:.1f} faltas. Por el esquema rival, su mejor valor"
            f" estadístico es: {market_label}."
        )

        players_result.append({
            "name": name,
            "pos": pos,
            "team": "HOME" if is_home else "AWAY",
            "teamName": team_data.get("team", {}).get("name", ""),
            "avgShots": round(l_shots, 2),
            "avgFouls": round(l_fouls, 2),
            "probShots15": prob_shots15,
            "probSOT05": prob_sot05,
            "probGoal": prob_goal,
            "probAst": prob_ast,
            "probGoalOrAst": prob_goal_or_ast,
            "probFouls15": prob_fouls15,
            "probCard": prob_card,
            "suggestedMarket": market_label,
            "betplayOdd": betplay_odd,
            "rushbetOdd": rushbet_odd,
            "bestBookie": best_bookie,
            "bestOdd": best_odd,
            "maxEV": max_ev,
            "writtenAnalysis": player_written_analysis,
        })

  # Generar Apuesta Combinada de Alto Valor
  top_players = sorted(players_result, key=lambda x: x["maxEV"], reverse=True)[
      :2
  ]
  combined_bet = None
  if len(top_players) >= 2:
    combined_odd = round(top_players[0]["bestOdd"] * top_players[1]["bestOdd"], 2)
    combined_bet = {
        "leg1": (
            f"{top_players[0]['name']} ({top_players[0]['suggestedMarket']})"
        ),
        "leg2": (
            f"{top_players[1]['name']} ({top_players[1]['suggestedMarket']})"
        ),
        "combinedOdd": combined_odd,
        "recommendedStake": "1.5% al 3% del Bankroll",
    }

  final_response = {
      "tacticalAnalysis": tactical_analysis,
      "players": players_result,
      "combinedBet": combined_bet,
  }

  cache_store[fixture] = (final_response, now)
  return final_response
    
