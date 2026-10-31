# Наблюдение Spot paper — Cloud Shell

Отдельный checkout `~/Trading_Bot_paper_observation`, база `~/Trading_Bot_paper_state/runtime-paper.db`. Запуск 31 октября 2026 в 15:54:31 Asia/Baku, BTCUSDT, 15m, максимум 10 виртуальных USDT на вход. Фиксированный начальный срок 14 дней, промежуточная проверка через сутки; не подбирать параметры на этих результатах. Действующие настройки Telegram читаются только защищённым launcher; значения не записывать в команды, отчёты и Git.

Проверка на Cloud Shell:

```bash
tmux has-session -t =trading-bot-paper-observation && echo 'paper running'
tmux has-session -t =trading-bot-paper-monitor && echo 'monitor running'
cat ~/Trading_Bot_paper_state/runtime-health.json
cat ~/Trading_Bot_paper_state/monitor/daily-report.json
```

Проверяйте возраст `time`/`quote.time`, paused, blocked, error и telegram_error; сам факт процесса недостаточен. Отчёты `monitor/report-YYYY-MM-DD.json` перезаписываются в течение дня по Баку и содержат накопительные результаты с начала наблюдения, а не только PnL дня. `runtime_orders` и `runtime_fills` в SQLite — журнал ордеров и исполнений. Не допускайте параллельный запуск и не используйте старую базу.

Остановка нового приложения (не старого):

```bash
kill -INT "$(tmux list-panes -t =trading-bot-paper-observation -F '#{pane_pid}')"
```

Проверить исчезновение сессии и ожидать завершения: `tmux has-session -t =trading-bot-paper-observation`. Отдельный монитор можно остановить `tmux kill-session -t =trading-bot-paper-monitor`. Повторный запуск приложения из отдельного checkout всегда на паузе:

```bash
cd ~/Trading_Bot_paper_observation
tmux new-session -d -s trading-bot-paper-observation 'exec env FUTURES_OUTBOX_DB="$HOME/Trading_Bot_futures_state/futures-paper.db" python3 -B deploy/cloud_paper_launch.py >> "$HOME/Trading_Bot_paper_state/runtime.log" 2>&1'
```

После проверки здоровья и сверки отдельная новая команда владельца `/resume` или, только для paper и после проверки `blocked`/pending, `kill -USR1 "$(tmux list-panes -t =trading-bot-paper-observation -F '#{pane_pid}')"` разрешает входы. После сбоя или перезапуска входы не возобновляются автоматически. `/pause` запрещает новые входы, не закрывая позицию. Риск-контроль существующей позиции продолжается.

Монитор запускается отдельно, если его сессия отсутствует:

```bash
cd ~/Trading_Bot_paper_observation
tmux new-session -d -s trading-bot-paper-monitor 'exec python3 -B -m trading_bot.paper_monitor --db "$HOME/Trading_Bot_paper_state/runtime-paper.db" --state-dir "$HOME/Trading_Bot_paper_state/monitor" >> "$HOME/Trading_Bot_paper_state/monitor.log" 2>&1'
```

Он сохраняет снимки примерно раз в минуту и создаёт проверенный SQLite backup `monitor/backup-latest.db` при старте и далее примерно ежечасно. Одной копии на том же диске недостаточно для аварийного восстановления. Проверяйте возраст отчёта и backup, а также `monitor/monitor-error.json` при его наличии. Результат drawdown основан на свежих выборочных котировках и может пропустить экстремумы; данные до запуска монитора и во время его остановки не восстановлены. `estimated_observed_seconds` — оценка по выборкам, не точный uptime; ошибки считаются выборками, а не уникальными инцидентами. Закрытия и gross PnL рассчитаны для текущей paper-модели комиссий USDT; прочие активы блокируют отчёт. Неизвестное не считается нулём.

Tmux переживает закрытие SSH, но не остановку/перезапуск самой Cloud Shell. В этом случае оба процесса и наблюдение останавливаются. Нужны ручная проверка, перезапуск на паузе и отдельное разрешение входов. Это не 24/7 сервер и не подтверждение прибыльности. Spot Testnet ключи и реальные средства не используются; будущий Testnet-прогон требует отдельной проверки и отдельных тестовых ключей.

## Telegram update (deployed 2026-11-01)

/status and /signal become human-readable Russian reports. /timeframe shows the
current period; /timeframe 1m, /timeframe 5m, /timeframe 15m or /timeframe 1h selects
it only on pause, flat and with no unresolved orders/gate/close request. Selection
persists after restart; entries stay paused. It resets the cached signal, not
trade history. Do not change the current 15m parameters during the fixed observation
just to improve its results: record a new experiment segment if changing them.
The report/timeframe update was deployed in Cloud Shell on 2026-11-01; see DEPLOYMENT_2026_11_01.md for the technical interruption and tests. No code hot-reload is assumed.
