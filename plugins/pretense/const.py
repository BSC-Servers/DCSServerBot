from core import get_translation

_ = get_translation(__name__.split('.')[1])


PRETENSE_RANKS = {
    1: {"code": "E-1", "name": _("E-1 Airman basic"), "requiredXP": 0},
    2: {"code": "E-2", "name": _("E-2 Airman"), "requiredXP": 2416},
    3: {"code": "E-3", "name": _("E-3 Airman first class"), "requiredXP": 10008},
    4: {"code": "E-4", "name": _("E-4 Senior airman"), "requiredXP": 22980},
    5: {"code": "E-5", "name": _("E-5 Staff sergeant"), "requiredXP": 41445},
    6: {"code": "E-6", "name": _("E-6 Technical sergeant"), "requiredXP": 65484},
    7: {"code": "E-7", "name": _("E-7 Master sergeant"), "requiredXP": 95161},
    8: {"code": "E-8", "name": _("E-8 Senior master sergeant"), "requiredXP": 130528},
    9: {"code": "E-9", "name": _("E-9 Chief master sergeant"), "requiredXP": 171627},
    10: {"code": "O-1", "name": _("O-1 Second lieutenant"), "requiredXP": 218499},
    11: {"code": "O-2", "name": _("O-2 First lieutenant"), "requiredXP": 271177},
    12: {"code": "O-3", "name": _("O-3 Captain"), "requiredXP": 329691},
    13: {"code": "O-4", "name": _("O-4 Major"), "requiredXP": 394071},
    14: {"code": "O-5", "name": _("O-5 Lieutenant colonel"), "requiredXP": 464341},
    15: {"code": "O-6", "name": _("O-6 Colonel"), "requiredXP": 540524},
    16: {"code": "O-7", "name": _("O-7 Brigadier general"), "requiredXP": 622644},
    17: {"code": "O-8", "name": _("O-8 Major general"), "requiredXP": 710721},
    18: {"code": "O-9", "name": _("O-9 Lieutenant general"), "requiredXP": 804773},
    19: {"code": "O-10", "name": _("O-10 General"), "requiredXP": 904819}
}

RANK_CODES = {rank["code"]: level for level, rank in PRETENSE_RANKS.items()}


def get_rank_for_xp(xp: int) -> tuple[int | None, dict | None]:
    for rank in reversed(list(PRETENSE_RANKS.keys())):
        if xp >= PRETENSE_RANKS[rank]["requiredXP"]:
            return rank, PRETENSE_RANKS[rank]
    return None, None


def get_rank_name_for_xp(xp: int) -> str | None:
    _, rank_data = get_rank_for_xp(xp)
    return rank_data["name"] if rank_data else None
