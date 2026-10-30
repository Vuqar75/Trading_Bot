# Временная проверка в Google Cloud Shell

Загрузите trading-bot-cloud-shell.zip через меню Upload в домашнюю папку.
Распакуйте в НОВУЮ папку, чтобы не перезаписать существующие файлы:

```bash
cd ~
mkdir trading-bot-demo
cd trading-bot-demo
unzip ~/trading-bot-cloud-shell.zip
cd Trading_Bot
python3 -m unittest discover -s tests -v
python3 cloud_shell_start.py
```

Если trading-bot-demo уже существует, выберите другое имя новой папки.
При запуске вставьте токен BotFather в скрытый запрос, нажмите Enter,
затем введите ваш числовой Telegram ID. Токен не вводится в командную строку
и не сохраняется в файл. Настройки из другой облачной среды сюда не переносятся.
Скрипт принудительно выбирает paper, отключает ИИ, использует отдельную базу
data/cloud-paper.db и включает паузу перед запуском.

Откройте бота в Telegram, нажмите Start, отправьте /status.
Ожидается paper, Spot, BTCUSDT (или ранее выбранная пара), Пауза: True.
/resume разрешает только виртуальные сделки; /pause запрещает новые входы.
/close закрывает виртуальную позицию и включает паузу. /stop не закрывает позицию.
Остановка процесса: Ctrl+C. Не запускайте одновременно другой экземпляр
с тем же Telegram-токеном: Telegram polling конфликтует.

Тесты должны показать 18 успешных проверок. Никаких pip install не требуется.
Cloud Shell временный: завершение сеанса может остановить бота; это не сервер 24/7.
Полное подключение из вашей сессии Cloud Shell проверяется после загрузки;
проверка serverTime подтверждает лишь доступность этого публичного endpoint.

## Обновление из GitHub

В Telegram отправьте /pause. В терминале остановите программу через Ctrl+C,
затем в папке проекта выполните:

```bash
git pull --ff-only origin main
python3 -m unittest discover -s tests -v
python3 cloud_shell_start.py
```

Снова введите токен и Telegram ID. База data/cloud-paper.db, история и позиция
сохраняются; не удаляйте папку data. После запуска проверьте /status и /history.
Каждый запуск скрипта включает паузу; /resume разрешает виртуальные входы.
