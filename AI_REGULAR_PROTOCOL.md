# Регулярный AI-shadow: протокол v1

Проверки начинаются только после установки отдельной авторизации. BTCUSDT,
публичные закрытые свечи и ограниченные новости; данные 1H/4H/15m, один
сохранённый снимок для модели и baseline. Граница — UTC, попытка допустима
лишь через 120–300 секунд после неё; пропущенные решения задним числом не
создаются. Режимы — TREND_UP, TREND_DOWN, RANGE, NO_TRADE. Входы запрещены.

Одна попытка на границу, не более шести в календарные UTC-сутки; gpt-6-luna,
до 2000 выходных токенов, резерв максимум $0,003640, общий лимит $0,25 с
учётом всех прежних резервов. HTTP не повторяется; неопределённость с
сохранённым резервом приостанавливает будущие вызовы. Сохранённый ответ
обрабатывается локально. Срок ответа — не позднее следующей 4H-границы;
завышенный моделью срок не продлевает решение. Данные времени события книги
не подтверждены и для допуска к исполнению не используются.

Сумма прежних резервов $0,007806; остаток $0,242194. Верхняя оценка при
шести максимальных резервах — $0,021840 в сутки, 66 попыток / 11 полных
суток до невозможности нового максимального резерва. Фактические резервы и
usage могут быть меньше; суммы резервов не равны счёту провайдера.

Считаются совпадения, расхождения, смены режимов и отказы, без утверждения о
правильности. Диагностические горизонты: 4H и 24H от границы решения;
ориентир — закрытие последней полностью закрытой 4H-свечи на границе,
результат — процентное изменение закрытия на точной конечной границе. Если
подходящая закрытая свеча своевременно не получена, исход неизвестен, не ноль.
Это описание направления цены, а не PnL и не доказательство преимущества.
Результаты до начала авторизации не включаются в prospective-сравнение.

Внешний архив журнала и locks создаётся согласованным SQLite backup API,
шифруется host-bound credential, публикуется только в отдельном prefix
существующего закрытого bucket и проверяется загрузкой и восстановлением.
Архив не является активным писателем. Восстановление host-bound шифрования на
другой VM не доказано; новые IAM-права и изменения торговых backups запрещены.
# Presentation and expiry clarification, 2026-11-06

For future requests every `reasons` and `uncertainties` item is requested in Russian. Saved responses are not translated through an additional model call. A decision expires at its model-provided `expires_ms` or the next 4H boundary, whichever is earlier; a short expiry is never extended. The display reads a separately published snapshot; its buttons do not collect data, call an AI endpoint, or change trade state. This clarification does not alter old responses, reservations or the existing boundary authorization.


## Regime question horizon v2 — future requests only

The fiscal authorisation remains `regular-shadow-v1` with the same cap, gates,
start and daily limits. New requests record `regular-regime-horizon-v2` in
`regular_versions.protocol`, alongside the exact request hash. No schema,
reservation, stored response, historical expiry or observation is rewritten.

There is no hardcoded fifteen-minute lifetime in the reviewed request or
publisher. The previous prompt gave the model an upper bound but did not
clearly distinguish the 4H regime classification from its supporting 15m
features. It did not record why the model selected a short lifetime, so that
reason is unknown; a short lifetime alone is not proof of a broken timer.

The v2 question supplies the exact next review in UTC and milliseconds. It
asks for the broad 4H regime, with that scheduled expiry when no concrete
earlier invalidation is supported. If evidence requires an earlier expiry,
the model retains it and must explain the shorter horizon in Russian
uncertainties. NO_TRADE remains valid. This is prompt guidance, not a guarantee
that a future answer will last four hours. Local bounds and rejection rules
are unchanged; no extra request is sent just to obtain a longer answer.

A regime label's lifetime does not make the original quote, order-book
snapshot or news newly fresh. It is neither a guaranteed prediction nor a
trade-entry permission. All adaptive entries remain disabled.

Deployment: only the regular server executor changes. Through the existing
operator connection, stage this exact candidate, pause its timer briefly
outside the request window, wait for the service to be inactive, switch only
the regular executor's code reference, then restore its previously enabled
timer. Do not restart trading workers, publisher or observer, change fiscal
authorisation, reset the journal or test with a paid call. Confirm the installed
path/hash and next authorised boundary. Use targeted tests; the full trading
application suite is not required for this server-only prompt change.

Verification in development: 20 tests in tests.test_adaptive_regular passed,
including request immutability, boundary validation, version/request hash,
one-request idempotence and exact preservation of a shorter model expiry.
No live API request and no VM change were performed here. The next normally
scheduled answer is the first opportunity to observe this prompt revision.
