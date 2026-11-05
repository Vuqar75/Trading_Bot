# Изолированный Donchian60 на существующей paper VM

Эта инструкция относится только к отдельному публичному наблюдателю; торговые
службы, их базы, Telegram, параметры и паузы не меняются. Стратегия и исходная
дата не меняются; метка источника котировки V2 и её срок действия описаны в
`DONCHIAN60_EXECUTION_V2.md` и атомарно записываются в SQLite до применения.

Предусмотрены `donchian-observer.service` под отдельным `donchian` и
`donchian-backup.service`/`.timer` под `donchianbackup`. Наблюдатель не имеет
доступа к каталогу `/var/lib/tradingbot` или к metadata server; firewall-проверка
должна подтвердить фактический запрет. Исходный файл стратегии не меняется;
adapter `research/donchian_service.py` проверяет его SHA-256 и обеспечивает
единственный writer через flock. Systemd ограничивает ресурсы, количество
процессов, частоту логов и число рестартов; остановка обрабатывается до выхода
из цикла. При неопределённом исходе quote либо пропущенном окне старая
блокировка сохраняется, ретроспективные fills не создаются.

Доступные операционные команды после установки:

```sh
systemctl status donchian-observer.service
sudo -u donchian env PYTHONPATH=/opt/donchian-observer /usr/bin/python3 -B -m research.donchian_prospective status --state /var/lib/donchian-observer/observer.sqlite
sudo journalctl -u donchian-observer.service --since '2 hours ago'
systemctl list-timers donchian-backup.timer
sudo cat /var/lib/donchian-backup/success.json
sudo systemctl stop donchian-observer.service
sudo systemctl start donchian-observer.service
```

`active` подтверждает лишь процесс. Health `/var/lib/donchian-observer/health.json`
содержит heartbeat и отдельные времена данных, состояние пропусков и блокировки;
устаревшие значения или пустая оценка не считаются здоровыми. При изменении
источника или пропуске не удалять claims и не менять SQLite вручную.

Ежедневная SQLite-копия через backup API упаковывается в единственный объект
`paper-backups/daily/donchian60/` существующего закрытого bucket. Prefix попадает
под **уже существующее** правило bucket 14 дней, затем soft delete 7 дней;
IAM, lifecycle и торговые копии не меняются. В архиве только база и manifest
со временем, SHA-256, исходным стартом, watermark, версиями и счётчиками.
Служба проверяет повторное скачивание объекта, hash, integrity_check и
содержимое перед записью success marker; отсутствие успеха не маскируется
старым локальным архивом. Восстанавливать только в изолированный каталог,
не запуская второй сборщик.

Перенос: вне 5-минутного окна UTC подтвердить health и отсутствие claims,
остановить Cloud Shell и убедиться в отсутствии процесса/автозапуска; выполнить
SQLite backup, SHA-256 и integrity_check; перенести в отдельную VM-базу,
проверить старт, все счета, свечи, сигналы, события и watermark; только затем
запустить VM-сервис и проверить сохранность и единственность. При сбое сначала
остановить новый экземпляр и сохранить новую базу; старый snapshot нельзя
восстанавливать поверх новых событий. Cloud Shell исходник остаётся архивом.
Перезагрузка рабочей VM ради испытания не производится.
