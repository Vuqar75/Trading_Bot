# Изолированный Donchian60: эксплуатация

Наблюдатель работает отдельно от paper VM и не импортирует торговые точки входа,
не подключается к Telegram, финансовым базам и приватным API. Перед запуском
сверить [протокол](DONCHIAN60_PROSPECTIVE_PROTOCOL.md) и зафиксированный коммит.
В Cloud Shell код расположен в `~/donchian60-prospective/code`, состояние в
`~/donchian60-prospective/state/observer.sqlite`; каталог доступен только владельцу.
Процесс получает очищенное окружение `env -i`, исключающее секреты.

Проверка (не запускает второй процесс):

```sh
cd ~/donchian60-prospective/code
tmux has-session -t donchian60-observer && echo RUNNING || echo STOPPED
PYTHONPATH=. python3 -B -m research.donchian_prospective status --state "$HOME/donchian60-prospective/state/observer.sqlite"
```

Восстановить работу только с тем же точным кодом и существующей базой; повторная
инициализация запрещена. Перед продолжением проверить `halted`, `error`, возраст
последней свечи и backup. Просроченный интервал или неопределённая операция
остаются заблокированными; их нельзя превращать в ретроспективные сделки.

```sh
cd ~/donchian60-prospective/code
tmux new-session -d -s donchian60-observer "exec env -i HOME=$HOME PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONPATH=. /usr/bin/python3 -u -B -m research.donchian_prospective daemon --state $HOME/donchian60-prospective/state/observer.sqlite >> $HOME/donchian60-prospective/state/observer.log 2>&1"
```

Одновременный второй процесс с той же базой отклоняется блокировкой. Остановка:
`tmux kill-session -t donchian60-observer`. При остановке во время получения
котировки незавершённая операция после перезапуска блокирует расчёты; проверка
такого инцидента требует отдельного решения, а не ручного удаления записей.

Проверить согласованную копию можно командой:
`PYTHONPATH=. python3 -B -m research.donchian_prospective backup --state "$HOME/donchian60-prospective/state/observer.sqlite"`.
Копия `observer.sqlite.backup` формируется через SQLite backup API и проверяется
`integrity_check`. Для восстановления сначала проверить копию в новом каталоге;
не перезаписывать действующую или более новую базу. До замены исходной базы
сначала исследовать и сохранить все последние события.

Проверка восстановления в отдельном месте без запуска наблюдателя:

```sh
mkdir -m 700 "$HOME/donchian60-prospective/restore-check"
cp "$HOME/donchian60-prospective/state/observer.sqlite.backup" "$HOME/donchian60-prospective/restore-check/observer.sqlite"
python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("PRAGMA integrity_check").fetchone()[0])' "$HOME/donchian60-prospective/restore-check/observer.sqlite"
PYTHONPATH=. python3 -B -m research.donchian_prospective status --state "$HOME/donchian60-prospective/restore-check/observer.sqlite"
```

Не запускать восстановленную копию одновременно с оригиналом. Если оригинал
потерян, продолжать с проверенной копии только после остановки прежнего процесса
и оценки событий, которые могли появиться после backup; пропущенное окно нельзя
заполнить ретроспективными сделками.

Cloud Shell и tmux могут работать после закрытия SSH, но остановка или ограничение
самой среды прерывает наблюдение. Это не гарантированный круглосуточный сервис.
Задержку, пропущенные окна и неопределённые оценки показывать явно. Напоминания,
внешние уведомления и платные ресурсы не создаются.
