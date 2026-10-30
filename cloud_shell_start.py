"""Временный интерактивный запуск симуляции в Linux / Cloud Shell."""
import getpass
import os
import argparse
import json
import tempfile
from pathlib import Path


def credentials(path, reset=False):
    if path.is_symlink():
        raise SystemExit("Файл настроек не должен быть символической ссылкой.")
    if path.exists() and not reset:
        try:
            path.chmod(0o600)
            saved = json.loads(path.read_text())
            token, owner = saved["token"], saved["owner"]
            if not isinstance(token, str) or not token.strip() or not isinstance(owner, str) or not owner.isdigit():
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("Не удалось прочитать настройки. Запустите с --reset-login для повторного ввода.")
        print("Используются сохранённые настройки Telegram.")
        return token, owner
    token = getpass.getpass("Вставьте токен Telegram (ввод скрыт): ").strip()
    owner = input("Ваш числовой Telegram ID: ").strip()
    if not token or not owner.isdigit():
        raise SystemExit("Нужны токен и числовой Telegram ID.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = None
    try:
        # mkstemp creates the file with 0600 before any secret is written.
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump({"token": token, "owner": owner}, stream)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    print("Настройки Telegram сохранены вне проекта, с правами доступа 600.")
    return token, owner


def main():
    parser = argparse.ArgumentParser(description="Запуск виртуальной торговли в Cloud Shell")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--reset-login", action="store_true", help="Заново ввести токен и ID")
    group.add_argument("--forget-login", action="store_true", help="Удалить сохранённый токен и ID и выйти")
    args = parser.parse_args()
    login_path = Path.home() / ".config" / "trading-bot" / "telegram.json"
    if args.forget_login:
        login_path.unlink(missing_ok=True)
        print("Сохранённые настройки удалены. Бот не запущен.")
        return
    os.chdir(Path(__file__).resolve().parent)
    print("Запуск симуляции paper. Ключи Binance не нужны. ИИ отключён.")
    token, owner = credentials(login_path, reset=args.reset_login)
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
    print("Для остановки программы нажмите Ctrl+C. Для смены токена: --reset-login.")
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
