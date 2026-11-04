# Раздельные Spot и futures paper в Cloud Shell

Проверенный исходный код: `6026e45de9cfa712e79e48d7db18e99df5bc8428`, два отдельных checkout. Полный набор: Cloud Shell 233/233, WSL Linux 233/233. Никаких биржевых ордеров и реальных средств.

## Spot

`~/Trading_Bot_paper_observation`, база `~/Trading_Bot_paper_state/runtime-paper.db`, сессия `trading-bot-paper-observation`; Telegram — только существующий защищённый launcher. До обновления входы были разрешены, позиция отсутствовала, pending=0, интервал 15m, код `e277d7baddf264b470c4e063beee414336c46921`. Пауза для обновления зафиксирована 2026-11-01 00:46:00 Asia/Baku, процесс остановлен корректно. Согласованный backup `~/Trading_Bot_paper_state/backup-before-update-20261101.db` прошёл integrity_check; 0 ордеров/0 fills в снимке и после запуска. Обновление без незакоммиченных изменений, история не удалялась.

Spot запущен на паузе. После успешного цикла: BTCUSDT 15m, pending=0, ошибок/блокировок нет, один Spot-процесс, свежая котировка. Три тестовых отчёта `/status`, `/signal`, `/timeframe` сформированы кодом новой версии на отдельном временном SQLite-снимке; проверен ожидаемый формат и 15m, реальный Telegram API подтвердил отправку владельцу. Это проверка формирования и доставки, а не приём настоящих входящих команд; пользовательская проверка ещё может дополнить её. После повторной проверки health/pending/blocked прежнее разрешение paper-входов явно восстановлено SIGUSR1 в 00:50:22 Asia/Baku; health подтвердил paused=false. Технический перерыв 00:46:00–00:50:22, около 4 мин 22 с. Монитор `trading-bot-paper-monitor` остаётся отдельным процессом, отчёт свежий, backup integrity_check=ok. См. PAPER_OBSERVATION_RUNBOOK.md.

## Futures

Публичный USD-M Futures доступен в Cloud Shell; `--once` exit 0 на паузе со свежей mark, демо exit 0 и ожидаемым net `0.4002`. Отдельный checkout `~/Trading_Bot_futures_paper`, база `~/Trading_Bot_futures_state/futures-paper.db`, журнал `~/Trading_Bot_futures_state/futures.log`, сессия `trading-bot-futures-paper`. После проверки здоровья включён SIGUSR1 2026-11-01 00:47:46 Asia/Baku: BTCUSDT, isolated paper, 1×, маржа 10 виртуальных USDT, 15m, комиссия 5 bps/сторона, adverse slippage 2 bps/сторона, maintenance 50 bps. Отдельного тестового Telegram-токена в окружении не обнаружено, futures работает без Telegram и не использует Spot-токен.

SQLite-снимок `~/Trading_Bot_futures_state/futures-backup.db` прошёл integrity_check; отдельная tmux-сессия `trading-bot-futures-backup` запускает согласованный backup каждый час. Копия на том же диске не защищает от потери диска. При ошибке backup проверяйте `backup.log` и возраст копии.

```bash
tmux has-session -t =trading-bot-futures-paper && echo 'futures running'
tmux has-session -t =trading-bot-futures-backup && echo 'backup loop running'
cat ~/Trading_Bot_futures_state/futures-health.json
kill -INT "$(tmux list-panes -t =trading-bot-futures-paper -F '#{pane_pid}')"
```

Перезапуск без автоматических входов (сначала убедитесь, что старой сессии нет):

```bash
cd ~/Trading_Bot_futures_paper
tmux new-session -d -s trading-bot-futures-paper 'exec python3 -B -m trading_bot.futures_app --db "$HOME/Trading_Bot_futures_state/futures-paper.db" --margin 10 --fee-bps 5 --slippage-bps 2 --maintenance-bps 50 --allow-paper-signal >> "$HOME/Trading_Bot_futures_state/futures.log" 2>&1'
```

После перезапуска сначала проверить свежесть health, ошибки, позицию и условия рынка; входы на паузе, SIGUSR1 — отдельное явное действие. Работа по tmux переживает закрытие SSH, но не остановку Cloud Shell. Нет биржевых защитных ордеров; модель funding может не учесть события, опубликованные после закрытия. Результаты не доказывают прибыльность. Не создавать платную VM и не подключать настоящие/тестовые ордера без нового разрешения.
