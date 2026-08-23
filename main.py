from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import math

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def prob_at_least(k, lambd):
    total = 0
    for i in range(k):
        total += (math.pow(lambd, i) * math.exp(-lambd)) / math.factorial(i)
    return 1 - total

def get_hash_factor(name: str):
    hash_val = sum(ord(c) for c in name)
    return 0.8 + ((hash_val % 40) / 100)

@app.get("/api/matches")
def get_matches():
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
    res = requests.get(url).json()
    
    events = res.get("events", [])
    matches = []
    
    for ev in events:
        id_match = ev["id"]
        name = ev["name"]
        status = ev["status"]["type"]["shortDetail"]
        matches.append({"id": id_match, "name": name, "status": status})
        
    return {"matches": matches}

@app.get("/api/analysis/{match_id}")
def analyze_match(match_id: str):
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={match_id}"
    res = requests.get(url).json()
    
    lineups = res.get("rosters", [])
    players_data = []

    for team in lineups:
        team_name = team.get("team", {}).get("displayName", "Equipo")
        roster = team.get("roster", [])

        for p in roster:
            player_info = p.get("athlete", {})
            name = player_info.get("displayName", "Jugador")
            pos = player_info.get("position", {}).get("abbreviation", "M")
            
            if pos in ["GK"]:
                pos_code = "G"
            elif pos in ["CB", "LB", "RB", "DF"]:
                pos_code = "D"
            elif pos in ["FW", "ST", "LW", "RW"]:
                pos_code = "F"
            else:
                pos_code = "M"

            factor = get_hash_factor(name)
            
            base_sot = 1.45 if pos_code == 'F' else 0.85 if pos_code == 'M' else 0.35 if pos_code == 'D' else 0.01
            base_goal = 0.42 if pos_code == 'F' else 0.18 if pos_code == 'M' else 0.08 if pos_code == 'D' else 0.001
            base_ast = 0.22 if pos_code == 'F' else 0.28 if pos_code == 'M' else 0.10 if pos_code == 'D' else 0.01
            base_fouls = 1.1 if pos_code == 'F' else 1.4 if pos_code == 'M' else 1.6 if pos_code == 'D' else 0.2
            base_cards = 0.12 if pos_code == 'F' else 0.18 if pos_code == 'M' else 0.25 if pos_code == 'D' else 0.05

            lambda_sot = base_sot * factor
            lambda_goal = base_goal * factor
            lambda_ast = base_ast * factor
            lambda_fouls = base_fouls * factor
            lambda_cards = base_cards * factor

            prob_sot05 = prob_at_least(1, lambda_sot)
            prob_sot15 = prob_at_least(2, lambda_sot)
            prob_goal = prob_at_least(1, lambda_goal)
            prob_ast = prob_at_least(1, lambda_ast)
            prob_fouls15 = prob_at_least(2, lambda_fouls)
            prob_card = prob_at_least(1, lambda_cards)

            suggested = "+0.5 Remate Puerta"
            chosen_prob = prob_sot05
            if pos_code == 'F' and prob_sot15 >= 0.40:
                suggested = "+1.5 Remates Puerta"
                chosen_prob = prob_sot15
            elif pos_code == 'D' and prob_fouls15 >= 0.45:
                suggested = "+1.5 Faltas Cometidas"
                chosen_prob = prob_fouls15

            betplay_odd = round((1 / chosen_prob) * 1.08, 2)
            rushbet_odd = round((1 / chosen_prob) * 1.05, 2)

            ev_betplay = round(((chosen_prob * betplay_odd) - 1) * 100, 1)
            ev_rushbet = round(((chosen_prob * rushbet_odd) - 1) * 100, 1)

            best_bookie = "Betplay" if betplay_odd >= rushbet_odd else "Rushbet"
            best_odd = max(betplay_odd, rushbet_odd)
            max_ev = max(ev_betplay, ev_rushbet)

            players_data.append({
                "name": name,
                "pos": pos_code,
                "team": team_name,
                "probSOT05": round(prob_sot05 * 100, 1),
                "probSOT15": round(prob_sot15 * 100, 1),
                "probGoal": round(prob_goal * 100, 1),
                "probAst": round(prob_ast * 100, 1),
                "probFouls15": round(prob_fouls15 * 100, 1),
                "probCard": round(prob_card * 100, 1),
                "suggestedMarket": suggested,
                "betplayOdd": betplay_odd,
                "rushbetOdd": rushbet_odd,
                "bestBookie": best_bookie,
                "bestOdd": best_odd,
                "maxEV": max_ev
            })

    return {"players": players_data}
          
