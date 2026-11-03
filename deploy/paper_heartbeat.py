"""One heartbeat; no orders, Telegram polling, or financial DB access."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading_bot.paper_heartbeat import send

send()
print('Paper VM heartbeat submitted; trading health checked separately.',flush=True)
