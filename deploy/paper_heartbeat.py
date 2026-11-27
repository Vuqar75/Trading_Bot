"""Publish liveness and protection without touching the trading SQLite."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_heartbeat import bracket_health,send

health=bracket_health()
send(protection=health)
print(f'Paper VM heartbeat submitted; bracket protection state {health}.',flush=True)
