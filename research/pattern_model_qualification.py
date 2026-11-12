"""Research model eligibility, separate from scores and trading permissions.

This registry contains no admitted model. Model files and snapshots cannot
self-certify by setting an `admitted`, `qualification` or `trade_allowed` flag.
Adding a new model identity alone never passes economic validation.
"""

PATTERN_MODEL_ID = '2a0ecbe3351f44f36ce59c336b4e28d089841de21fa5ec915276117fe3d713bc'


def qualification(model_id):
    known = isinstance(model_id, str) and model_id == PATTERN_MODEL_ID
    return {
        'status': 'failed_economic_validation' if known else 'not_evaluated',
        'candidate_allowed': False,
        'reason': 'walk_forward_selection_not_profitable' if known else 'no_verified_qualification',
        'evidence': ['PATTERN_AI_FORECAST_QUALITY.md', 'MODEL_FLATNESS_AUDIT_20261112.md'] if known else [],
    }


def explanation(model_id):
    if qualification(model_id)['status'] == 'failed_economic_validation':
        return 'ML не прошёл проверку отбора прибыльных входов; рекомендации сделки отключены.'
    return 'Качество ML для выбора сделки не подтверждено; рекомендации сделки отключены.'
