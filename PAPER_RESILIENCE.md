# Margin, внешние копии и мониторинг — кандидат 3 ноября 2026

Состояние и статус установки — в начале HANDOFF.md. Исторические ValueError
нельзя восстановить по типу исключения: код прежде не сохранял сообщение и
стадию. Новая диагностика записывает ограниченные стадию, символ, интервал,
тип, имя файла и номер строки, но не строку исключения, заголовки, URL или тела
ответов. Маржинальные входы и futures cross остаются на паузе.

## Проект внешней копии (пока не подключён)

`paper_external_backup.prepare` использует согласованную блокировку и SQLite
backup трёх баз; архив содержит сами базы, их SHA-256, время, git-коммит и
SQLite user/schema versions. Исходные базы содержат финансовые настройки;
восстанавливать только в изолированный каталог без торговых процессов и без
Telegram-поллера, сверить журнал, позиции, займы, funding и очередь и отдельно
решить, как сохранить события, возникшие после снимка. Кодовая версия и
несекретные systemd unit-файлы восстанавливаются из указанного коммита Git.
Закрытую Telegram-конфигурацию нужно восстановить отдельно из защищённого
хранилища владельца; в архив она не включается и её доступность не доказана.

Рекомендуемый bucket: отдельный региональный Standard `europe-west1`, public
access prevention, uniform bucket-level access, без object versioning, без
необратимого retention lock. Lifecycle-файл `deploy/paper-backup-lifecycle.json`
удаляет только префиксы `paper-backups/hourly/` через 2 дня и `daily/` через
14 дней; выполнение lifecycle асинхронно, срок не гарантирует точную секунду.
По умолчанию soft delete дополнительно хранит удалённые объекты семь дней и
начисляет плату; его не отключать автоматически. Проверка загрузки делает
создание объекта с условием отсутствия и скачивание для сверки SHA-256;
маркер успеха записывается только после проверки.

На 3 ноября размер согласованной неупакованной копии: 1 597 809 байт.
Если консервативно считать каждый архив такого размера, в обычном состоянии
62 живых объекта (48 часовых + 14 суточных) занимают около 0,092 GiB; ещё до
175 объектов soft delete (7×24 + 7 дневных) дают около 0,353 GiB суммарно.
При ориентире Standard $0,02/GiB-месяц это около $0,0071 хранения, 750
загрузок/месяц по $0,005/1000 — $0,00375, 750 read-back по $0,0004/1000 —
$0,00030, один restore read — ~$0,0000004. Итого ориентир около $0,0112/месяц
без налогов, возможных дополнительных запросов CLI, сетевого трафика за пределы
региона, роста БД, асинхронности удаления и других услуг. Точная ставка региона
в интерактивной таблице или Billing SKU должна быть перепроверена перед
созданием: опубликованный здесь $0,02 — расчётное допущение, не счёт. Копии в
том же регионе защищают от потери диска/VM, но не от регионального отказа.

Источники: https://cloud.google.com/storage/pricing ;
https://cloud.google.com/storage/docs/lifecycle ;
https://cloud.google.com/storage/docs/soft-delete ;
https://cloud.google.com/storage/docs/access-control/iam-roles .

Фактическая VM не имеет прикреплённого service account; Storage API и Monitoring
API не были включены в проекте. Для автоматической загрузки потребуется отдельная
учётная запись с bucket-level `roles/storage.objectCreator` и
`roles/storage.objectViewer` (read-back; без удаления), корректный access scope и
включённый Storage API. Lifecycle удаляет свои объекты самостоятельно. По
документации Google прикрепление service account к существующей VM требует её
остановки; это отдельное переключение с остановкой защит и требует продуманного
окна. Нельзя тихо останавливать VM ради создания bucket. См.
https://cloud.google.com/compute/docs/instances/change-service-account .

После отдельного согласования платного bucket и окна обслуживания подготовлены
команды (имя bucket выбрать уникальное):

```bash
PROJECT=gen-lang-client-0854183882
BUCKET=ИМЯ-УНИКАЛЬНОГО-БАКЕТА
SA=tradingbot-backup-uploader@$PROJECT.iam.gserviceaccount.com
# Следующие команды меняют облачные ресурсы: выполнять только после согласования.
gcloud services enable storage.googleapis.com --project="$PROJECT"
gcloud iam service-accounts create tradingbot-backup-uploader --project="$PROJECT"
gcloud storage buckets create "gs://$BUCKET" --project="$PROJECT" --location=europe-west1 --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=deploy/paper-backup-lifecycle.json
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectCreator
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectViewer
# Только после безопасной остановки и согласованного окна:
gcloud compute instances set-service-account tradingbot-paper-test --project="$PROJECT" --zone=europe-west1-b --service-account="$SA" --scopes=cloud-platform
# После безопасного запуска и проверки исходных пауз:
python3 -B deploy/paper_external_backup.py --bucket "$BUCKET" --kind hourly
python3 -B deploy/paper_external_backup.py --bucket "$BUCKET" --kind daily
```

Прежде чем включать расписание, проверить реальные IAM права, отсутствие
непредусмотренных ролей, read-back, независимое восстановление и мониторинг
возраста внешней копии. Часовой и суточный таймеры для внешнего bucket в этом
кандидате не включены. Новых ресурсов и ключей не создавалось.

## Мониторинг

Локальный watchdog оценивает systemd, health, котировки, защиту, ошибки и
блокировки, очередь, локальные/внешние копии, память и диск. Причина паузы,
если явно не сохранена, помечается неизвестной; вручную установленная пауза
не считается сбоем в чистой модели. Состояния событий и очередь уведомлений
фиксируются атомарно в общей базе; повтор подавляется, восстановление сообщает
отдельно, отправку выполняет существующий Spot-relay, второго поллера нет.
Не считать локальный watchdog проверкой смерти VM: при недоступной VM он сам
не работает, а сообщения relay могут задержаться до восстановления Spot.

Для независимого обнаружения остановки подходит Google Cloud Monitoring TCP
uptime check с alert policy: нужно отдельно определить безопасный доступ
проверочных IP к порту и внешний канал, доставляющий уведомление владельцу.
Открывать новые порты автоматически нельзя; обычный Telegram-поллер на VM
не доставит предупреждение при её остановке. Google прямо предупреждает, что
алерт по `compute.googleapis.com/instance/uptime` не уведомляет об остановке VM:
https://cloud.google.com/monitoring/alerts/troubleshoot-missing-notifications .
На дату проверки uptime checks имеют 1 млн исполнений в месяц на проект и
$0,30/1000 сверх лимита; с сентября 2027 заявлена отдельная тарификация части
alerting. Число исполнений считается по регионам проверок; другие расходы и
доступность канала нужно проверить. https://cloud.google.com/products/observability/pricing
Ничего внешнего пока не включено и доставка владельцу при падении VM не доказана.
