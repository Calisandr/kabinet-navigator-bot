from __future__ import annotations


COMMANDS = [
    {"command": "start", "description": "открыть меню"},
    {"command": "yesterday", "description": "расписание на вчера"},
    {"command": "today", "description": "расписание на сегодня"},
    {"command": "tomorrow", "description": "расписание на завтра"},
    {"command": "next", "description": "ближайшие даты"},
    {"command": "date", "description": "расписание на дату"},
    {"command": "teacher", "description": "поиск преподавателя"},
    {"command": "teachers", "description": "список преподавателей"},
    {"command": "refresh", "description": "обновить таблицу"},
    {"command": "stats", "description": "статистика пользователей"},
    {"command": "help", "description": "помощь"},
]

COMMAND_SCOPE_TYPES = ("default", "all_private_chats")
COMMAND_LANGUAGE_CODES = (None, "ru")


def build_set_my_commands_payloads() -> list[dict[str, object]]:
    payloads = []
    for scope_type in COMMAND_SCOPE_TYPES:
        for language_code in COMMAND_LANGUAGE_CODES:
            payload: dict[str, object] = {
                "commands": COMMANDS,
                "scope": {"type": scope_type},
            }
            if language_code is not None:
                payload["language_code"] = language_code
            payloads.append(payload)
    return payloads
