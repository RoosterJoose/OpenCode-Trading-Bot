#!/usr/bin/env python3
"""Expectancy momentum: rolling 20-trade avg R vs all-time baseline."""
import sqlite3, json, statistics

def calc(path):
    try:
        c = sqlite3.connect(path)
        trades = c.execute("SELECT r_multiple FROM trades WHERE r_multiple IS NOT NULL ORDER BY id").fetchall()
        c.close()
        if len(trades) < 25: return None
        r = [float(x[0]) for x in trades]
        return {"rolling_20": round(statistics.mean(r[-20:]), 4), "all_time": round(statistics.mean(r), 4), "momentum": round(statistics.mean(r[-20:]) - statistics.mean(r), 4)}
    except Exception as e: return {"error": str(e)}

con = calc("/opt/hermes-trading-bot/data/hermes.db")
aggr = calc("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db")

c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
if con: c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", ("expectancy_momentum_cons", json.dumps(con)))
if aggr: c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", ("expectancy_momentum_aggr", json.dumps(aggr)))
c.commit(); c.close()
print(json.dumps({"cons": con, "aggr": aggr}))
