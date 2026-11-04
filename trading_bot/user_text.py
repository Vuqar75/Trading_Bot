"""User-facing terminology; unknown diagnostics stay out of chat."""
BLOCKS = {
    'SUBMISSION_UNKNOWN': 'Неизвестен результат отправки ордера. Идёт сверка; повторная отправка запрещена.',
    'RECOVERY_REQUIRED': 'После перезапуска требуется сверить ордера.',
    'RECOVERY_INCOMPLETE': 'Восстановление ещё не завершено.',
    'ORDER_PENDING': 'Ордер ещё не завершён или не полностью учтён.',
    'ACCOUNTING_CONTRADICTION': 'Обнаружено расхождение учёта. Новые операции заблокированы до проверки.',
    'CONTRADICTION': 'Данные учёта противоречат друг другу. Требуется проверка.',
    'ORDER_REJECTED_REVIEW': 'Биржа отклонила ордер. Перед новой попыткой нужна проверка причины.',
}


def block_text(value):
    return 'нет' if not value else BLOCKS.get(value, 'Операции заблокированы; требуется проверка журнала.')


def error_text(value):
    if not value:
        return 'нет'
    reason = str(value.get('reason') or value.get('type', '')) if isinstance(value, dict) else str(value)
    lower = reason.lower()
    if 'funding' in lower:
        return 'Не удалось подтвердить начисления funding. Входы на паузе.'
    if 'stale' in lower:
        return 'Рыночные данные устарели. Ожидаем свежие данные.'
    if 'request failed' in lower or 'timeout' in lower or 'http' in lower:
        return 'Не удалось получить ответ сервиса. Следующая проверка будет повторена.'
    if 'dust' in lower or 'notional below' in lower:
        return 'Остаток или сумма сделки меньше биржевого минимума.'
    if 'insufficient' in lower or 'balance' in lower:
        return 'Недостаточно средств либо баланс не совпадает с учётом.'
    if 'candles' in lower:
        return 'Недостаточно последовательных свежих свечей для стратегии.'
    if 'telegram' in lower:
        return 'Не удалось получить или доставить сообщение Telegram.'
    return 'Ошибка проверки или обработки данных. Технические подробности сохранены в журнале.'


SPOT_HELP = '''Команды Spot (BTCUSDT, без плеча):
/status — состояние, позиция, котировка и результат
/signal — средние SMA и причина решения
/history — последние 10 выходов из позиции
/report — результат за сегодня по Баку
/pause — запретить новые входы, продолжить контроль позиции
/stop — то же, что /pause; программу не выключает
/resume — разрешить входы после проверок
/close — запросить закрытие и поставить входы на паузу
/timeframe — показать период свечей
/timeframe 1m|5m|15m|1h — изменить период на паузе без позиции
/start или /help — эта справка'''

FUTURES_HELP = '''Команды виртуальных isolated futures:
/status — маржа, позиция, цена, результат и просадка
/signal — сигнал SMA и причина решения
/history — последние 10 закрытий
/pause или /stop — запретить входы, продолжить контроль позиции
/resume — разрешить виртуальные входы
/close — запросить закрытие и поставить входы на паузу
/leverage — показать плечо; /leverage 1..10 — выбрать
/timeframe — показать период; /timeframe 1m|5m|15m|1h — выбрать
Плечо и период меняются на паузе без позиции.
Это симуляция. Реальные деньги не используются.'''
