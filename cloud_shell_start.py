"""Временный интерактивный запуск симуляции в Linux / Cloud Shell."""
import getpass
import os
from pathlib import Path


def main():
    os.chdir(Path(__file__).resolve().parent)
    print("Запуск симуляции paper. Ключи Binance не нужны. ИИ отключён.")
    token = getpass.getpass("Вставьте токен Telegram (ввод скрыт): ").strip()
    owner = input("Ваш числовой Telegram ID: ").strip()
    if not token or not owner.isdigit():
        raise SystemExit("Нужны токен и числовой Telegram ID.")
    os.environ.update(TELEGRAM_BOT_TOKEN=token, TELEGRAM_OWNER_ID=owner,
                      BOT_MODE="paper", AI_ENABLED="false", DB_PATH="data/cloud-paper.db")
    # Lock before modifying state; also held by the bot for the process lifetime.
    import fcntl
    from trading_bot.core import Store
    Path("data").mkdir(exist_ok=True)
    with open("data/cloud-paper.db.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Бот с этой базой уже запущен.")
        store = Store("data/cloud-paper.db")
        store.set("paused", True)
        store.db.close()
    print("Каждый запуск начинает на паузе. В Telegram отправьте /status.")
    print("Для остановки программы нажмите Ctrl+C. Секрет не сохраняется в файл.")
    from trading_bot.__main__ import main as run
    try:
        run()
    except KeyboardInterrupt:
        print("\nПрограмма остановлена.")
    except Exception as exc:
        # Exception URLs may contain credentials: do not print their bodies.
        print("Ошибка запуска:", type(exc).__name__, "HTTP:", getattr(exc, "code", "—"))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
