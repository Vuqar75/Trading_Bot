"""Consistent, presentation-only status line for every paper market."""
import math
from .telegram_reports import when


def brief_status(*,paused,positions,stamp,now,problem=False):
    pause='входы на паузе' if paused else 'входы разрешены'
    valid=isinstance(stamp,(int,float)) and not isinstance(stamp,bool) and math.isfinite(stamp)
    fresh=valid and 0<=now-stamp<=120
    updated=when(stamp) if valid else 'нет данных'
    condition='требует проверки' if problem else ('норма' if fresh else 'данные устарели или отсутствуют')
    return f'{pause}; позиций: {positions}; обновление: {updated}; состояние: {condition}.'
