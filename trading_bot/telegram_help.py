"""Plain-text guide for the installed unified interface, without legacy aliases.

Each section is a separate durable outbox message below Telegram's size limit.
There is no Markdown parse mode in the sender; command syntax stays literal.
"""


def unified_start_message():
    return ('Здравствуйте! Этот бот помогает наблюдать за крипторынком и управлять '
            'виртуальными сделками в paper-режиме. Подробные команды: /help.')


def unified_help_messages(selected_market, *, experimental_pair=False):
    pages=(
        f'''[Справка 1/3] Выбор рынка и торговля
Выбранный рынок: {selected_market}
Все торговые операции сейчас PAPER: виртуальные средства.

ВЫБОР РЫНКА
/mode — показать выбранный рынок.
/mode spot — обычная Spot-торговля.
/mode futures — isolated futures.
/mode futures_cross — cross futures.
/mode margin_cross — Spot margin cross.
Переключение выбирает получателя следующих команд; другие рынки продолжают работать.

СОСТОЯНИЕ И ТОРГОВЛЯ
/status — краткая сводка всех четырёх рынков.
/status spot — подробный статус Spot.
/status futures — подробный статус isolated futures.
/status futures_cross — подробный статус cross futures.
/status margin_cross — подробный статус margin.
/signal — сигнал стратегии выбранного рынка.
/history — история выходов выбранного рынка.
/report — торговые результаты: сегодня по Баку и за всё время.
/pause — запретить новые входы выбранного рынка; защита позиций продолжается при доступных данных.
/resume — разрешить входы выбранного рынка после проверок.
/pause_all — запросить паузу входов всех четырёх рынков; дождитесь подтверждений.

ПАРЫ: ТЕКУЩЕЕ ОГРАНИЧЕНИЕ
Spot и isolated: BTCUSDT. Cross и margin: BTCUSDT и ETHUSDT.
/pair из старого приложения в едином интерфейсе пока не поддерживается. Выбор других пар требует восстановления; /mode пару не меняет.''',
        '''[Справка 2/3] Параметры и закрытие

ПАРАМЕТРЫ
/timeframe — показать период выбранного рынка.
/timeframe 15m — выбрать период; варианты: 1m, 5m, 15m, 1h.
/leverage — показать плечо (futures или margin).
/leverage 2 — выбрать плечо: futures 1–10×, margin 1–3×. В Spot плеча нет.
Период и плечо меняются на паузе, без открытых позиций, с дополнительными проверками. Изменение параметров не возобновляет входы.

ЗАКРЫТИЕ И ПОДТВЕРЖДЕНИЯ
/close — подготовить закрытие позиции выбранного рынка.
/close BTCUSDT — выбрать BTC-позицию в cross или margin.
/close ETHUSDT — выбрать ETH-позицию в cross или margin.
/confirm ID — подтвердить предложенное закрытие, перевод или погашение.
/cancel ID — отменить неподтверждённое предложение.
Используйте кнопки под предложением или идентификатор из ответа бота. Предложение имеет срок действия и привязано к конкретной операции; /close ещё не означает исполнение. Автоматические защитные выходы не требуют ручного подтверждения.

ВАЖНО
/stop не является командой паузы в едином интерфейсе. Используйте /pause или /pause_all.''',
        '''[Справка 3/3] Кошельки, переводы и займы

КОШЕЛЬКИ — ОБЩИЕ ДЛЯ ВСЕХ /mode
/wallets — краткие остатки всех кошельков.
/wallet spot — Spot и доступность перевода.
/wallet futures_isolated — кошелёк isolated futures.
/wallet futures_cross — кошелёк cross futures.
/wallet margin_cross — кошелёк margin.
/wallet reserve — общий резерв.
/transfer ИЗ В СУММА — подготовить перевод виртуальных USDT с подтверждением.
/transfers — последние переводы.
/balance_report — подробный общий отчёт за всю историю.
/balance_report today — общий отчёт за сегодня по Баку.
/balance_report all — общий отчёт за всю историю.
/report относится к выбранному рынку; /balance_report — ко всем средствам.

Пример предложения перевода:
/transfer futures_isolated margin_cross 10
Сам пример ничего не переводит. Средства, пауза и ограничения риска проверяются повторно при подтверждении. Обеспечение открытых позиций недоступно для перевода.

ЗАЙМЫ — ТОЛЬКО /mode margin_cross
/loans — активные займы, основной долг и проценты.
/interest — модельные ставки, начисления и следующий период.
/repay ID_ЗАЙМА КОЛИЧЕСТВО — подготовить погашение с подтверждением.
Количество задаётся в активе займа: BTC, ETH или USDT, из свободного остатка.

СПРАВКА
/help — полная справка в трёх сообщениях.
/donchian — результаты изолированного paper-наблюдения Donchian60.
/adaptive — виртуальные расчёты адаптивного исследовательского стенда.
/ai — состояние отдельного ИИ-аналитика.
/start — короткое приветствие.
Неизвестные данные не означают ноль. Займы, проценты, funding и ликвидации — paper-модели; прибыльность стратегии не доказана.''',
    )
    if experimental_pair:
        pages=list(pages)
        pages[0]=pages[0].replace(
            'ПАРЫ: ТЕКУЩЕЕ ОГРАНИЧЕНИЕ\nSpot и isolated: BTCUSDT. Cross и margin: BTCUSDT и ETHUSDT.\n/pair из старого приложения в едином интерфейсе пока не поддерживается. Выбор других пар требует восстановления; /mode пару не меняет.',
            'ВЫБОР ПАРЫ — ЭКСПЕРИМЕНТ PAPER\n/pair — показать пары выбранного рынка.\n/pair SOLUSDT — предложить пару Spot или isolated.\n/pair BTCUSDT SOLUSDT — предложить набор cross или margin.\nСмена требует паузы, проверки состояния и /confirm ID; входы остаются на паузе.')
        pages[1]=pages[1].replace('/close ETHUSDT — выбрать ETH-позицию в cross или margin.',
                                   '/close SOLUSDT — пример выбора SOL-позиции в cross или margin.')
        pages[2]=pages[2].replace('Количество задаётся в активе займа: BTC, ETH или USDT, из свободного остатка.',
                                   'Количество задаётся в активе конкретного займа из свободного остатка.')
        return tuple(pages)
    return pages
