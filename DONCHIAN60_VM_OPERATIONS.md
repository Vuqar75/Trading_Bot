# Изолированный Donchian60 на существующей paper VM

Для фактически установленного локального контроля, еженедельной проверки восстановления и команд просмотра см. `DONCHIAN60_MONITORING.md`. Торговые службы и наблюдатель при установке контроля не перезапускались.

## Фактическая установка 5 ноября 2026

На `tradingbot-paper-test` работает ровно один наблюдатель из кода
`cc6a4b2f7ea07487a8ac734ab9ed0f0234402b8f`, база
`/var/lib/donchian-observer/observer.sqlite`. Исходный старт сохранён:
2026-11-05 07:45:47.836 UTC / 11:45:47.836 Баку. Финальная согласованная копия
Cloud Shell сделана после останова в 08:22:05 UTC; V2 транзакционно вступила в
силу 08:27:12.913 UTC. Окно 00:00 UTC не пропущено. Исходный Cloud Shell архив
сохранён, старый процесс и сессия остановлены; ссылок в cron/user systemd не
обнаружено. Все 18 счетов и исходные 270 свечей/шесть событий сверены по
снимку; с переходом добавлено только событие версии. Стратегических сделок нет.

На VM сервис включён для запуска после reboot; наблюдатель изолирован пользователем
`donchian`, пределами 96 MiB/15% CPU/16 задач, запретом metadata и каталога
торговых данных. Изолированная проверка cgroup не смогла обратиться к metadata;
фактический процесс не имеет соответствующих секретных переменных и прав чтения
торговой финансовой базы. Для отдельного uploader используется **уже прикреплённый
VM service account**: его существующие bucket-права действуют на bucket целиком,
а не принудительно только на prefix. Прав и service account не добавляли.

Первый итоговый backup в GCS загружен 08:27:31 UTC; размер 31 811 байт,
SHA-256 `adcc3a80e798555c97928a1f8636649264175fbd1e4648479b0eaaad23c2a238`.
Скачанная копия проверена по hash, `integrity_check`, счетам, событиям и дате
старта в отдельном каталоге без запуска процесса. Таймер активен ежедневно
03:15 UTC, `Persistent=true`. Существующие правила lifecycle удаляют объекты
после 14 дней и далее действует soft delete 7 дней; очистка асинхронная.
Переносной snapshot и манифест лежат отдельно в `.../donchian60/migration/`.
Тестовые rehearsal-объекты в том же ограниченном prefix удалятся по этой политике.

При текущем размере 31 811 байт и 21 днях хранения: примерно 0,00062 GiB и
около $0,000013/мес за Standard-хранение; 30 загрузок и 30 проверочных чтений
при $0,005/1000 A и $0,0004/1000 B — около $0,000162/мес, вместе порядка
$0,0002/мес без налогов и возможного внешнего трафика. Фактическая сумма
изменится при росте базы и числа операций. Источник цен:
https://cloud.google.com/storage/pricing . Новые платные ресурсы не создавались.

До переноса свободно было примерно 527 MiB RAM и 15 GiB диска; после запуска
доступно примерно 494 MiB, observer использовал около 13 MiB. Публичные запросы
проверены с VM, возраст выбранных сделок составил 1,6–2,3 с; это измерения,
не гарантия будущей доступности. Торговые службы остались active с прежними PID.
Read-only проверка показала все четыре торговые паузы включёнными; health был
свежее 13 секунд, без отмеченной ошибки. Cross/margin сообщали `quotes_fresh`
и `protection_fresh`; файлы Spot/isolated не содержат отдельных таких полей,
поэтому эта проверка не доказывает время их последнего защитного цикла.
Reboot не выполнялся: автозапуск проверен конфигурацией `enabled` и systemd,
фактическая работа после перезагрузки остаётся непроверенной.

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

Пример отдельной проверки последнего внешнего объекта (с существующей VM SA):

```sh
sudo install -d -m 700 /var/lib/donchian-restore-check
sudo /usr/bin/python3 - <<'PY'
import hashlib, io, json, os, sys, tarfile, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, '/opt/donchian-observer')
from research import donchian_external_backup as b
marker=json.loads(Path('/var/lib/donchian-backup/success.json').read_text())
req=urllib.request.Request('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',headers={'Metadata-Flavor':'Google'})
with urllib.request.urlopen(req,timeout=5) as r: token=json.load(r)['access_token']
url='https://storage.googleapis.com/storage/v1/b/'+b.BUCKET+'/o/'+urllib.parse.quote(marker['object'],safe='')+'?alt=media'
with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as r: data=r.read(20000001)
assert hashlib.sha256(data).hexdigest()==marker['sha256']
manifest=b.verify(data)
with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as t: raw=t.extractfile('observer.sqlite').read()
fd=os.open('/var/lib/donchian-restore-check/observer.sqlite',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'wb') as f: f.write(raw)
print('verified_start_ms',manifest['start_ms'],'counts',manifest['counts'])
PY
```

Команда не запускает восстановленный экземпляр и откажется перезаписать уже
существующий проверочный файл. Для нового испытания выберите новый каталог.

Перенос: вне 5-минутного окна UTC подтвердить health и отсутствие claims,
остановить Cloud Shell и убедиться в отсутствии процесса/автозапуска; выполнить
SQLite backup, SHA-256 и integrity_check; перенести в отдельную VM-базу,
проверить старт, все счета, свечи, сигналы, события и watermark; только затем
запустить VM-сервис и проверить сохранность и единственность. При сбое сначала
остановить новый экземпляр и сохранить новую базу; старый snapshot нельзя
восстанавливать поверх новых событий. Cloud Shell исходник остаётся архивом.
Перезагрузка рабочей VM ради испытания не производится.
