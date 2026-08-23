from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import math

app = FastAPI()

# Permitir peticiones desde GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def poisson(k, lambd):
    return (math.pow(lambd, k) * math.exp(-lambd)) / math.factorial(k)

def prob_at_least(k, lambd):
    total = sum(poisson(i, lambd) for i in range(k))
    return 1 - total

def get_hash_factor(name: str):
    hash_val = sum(ord(c) for c in name)
    return 0.8 + ((hash_val % 40) / 100)

@app.get("/")
def home():
    return {"status": "Backend activo"}

@app.get("/analyze-fixture")
def analyze_fixture(fixture: str, api_key: str):
    headers = {"x-apisports-key": api_key}
    url = f"https://v3.football.api-sports.io/fixtures/lineups?fixture={fixture}"
    
    response = requests.get(url, headers=headers)
    data = response.json()
    
    players_result = []
    
    if "response" in data and len(data["response"]) > 0:
        for team_index, team_data in enumerate(data["response"]):
            is_home = (team_index == 0)
            start_xi = team_data.get("startXI", [])
            
            for item in start_xi:
                player_obj = item.get("player", {})
                pos = player_obj.get("pos", "M")
                name = player_obj.get("name", "Jugador")
                player_factor = get_hash_factor(name)

                base_sot = 1.45 if pos == 'F' else 0.85 if pos == 'M' else 0.35 if pos == 'D' else 0.01
                base_goal = 0.42 if pos == 'F' else 0.18 if pos == 'M' else 0.08 if pos == 'D' else 0.001
                base_ast = 0.22 if pos == 'F' else 0.28 if pos == 'M' else 0.10 if pos == 'D' else 0.01
                base_fouls = 1.1 if pos == 'F' else 1.4 if pos == 'M' else 1.6 if pos == 'D' else 0.2
                base_cards = 0.12 if pos == 'F' else 0.18 if pos == 'M' else 0.25 if pos == 'D' else 0.05

                lambda_sot = base_sot * player_factor
                lambda_goal = base_goal * player_factor
                lambda_ast = base_ast * player_factor
                lambda_fouls = base_fouls * player_factor
                lambda_cards = base_cards * player_factor

                prob_sot05 = prob_at_least(1, lambda_sot)
                prob_sot15 = prob_at_least(2, lambda_sot)
                prob_goal = prob_at_least(1, lambda_goal)
                prob_ast = prob_at_least(1, lambda_ast)
                prob_fouls15 = prob_at_least(2, lambda_fouls)
                prob_card = prob_at_least(1, lambda_cards)

                market_label = "+0.5 Remate Puerta"
                chosen_prob = prob_sot05
                if pos == 'F' and prob_sot15 >= 0.40:
                    market_label = "+1.5 Remates Puerta"
                    chosen_prob = prob_sot15
                elif pos == 'D' and prob_fouls15 >= 0.45:
                    market_label = "+1.5 Faltas Cometidas"
                    chosen_prob = prob_fouls15

                betplay_odd = round((1 / chosen_prob) * 1.08, 2) if chosen_prob > 0 else 1.01
                rushbet_odd = round((1 / chosen_prob) * 1.05, 2) if chosen_prob > 0 else 1.01

                ev_betplay = ((chosen_prob * betplay_odd) - 1) * 100
                ev_rushbet = ((chosen_prob * rushbet_odd) - 1) * 100

                best_bookie = 'Betplay' if betplay_odd >= rushbet_odd else 'Rushbet'
                best_odd = max(betplay_odd, rushbet_odd)
                max_ev = max(ev_betplay, ev_rushbet)

                players_result.append({
                    "name": name,
                    "pos": pos,
                    "team": 'HOME' if is_home else 'AWAY',
                    "teamName": team_data.get("team", {}).get("name", ""),
                    "probSOT05": prob_sot05,
                    "probSOT15": prob_sot15,
                    "probGoal": prob_goal,
                    "probAst": prob_ast,
                    "probFouls15": prob_fouls15,
                    "probCard": prob_card,
                    "suggestedMarket": market_label,
                    "betplayOdd": betplay_odd,
                    "rushbetOdd": rushbet_odd,
                    "bestBookie": best_bookie,
                    "bestOdd": best_odd,
                    "maxEV": max_ev
                })

    return {"players": players_result}
                
