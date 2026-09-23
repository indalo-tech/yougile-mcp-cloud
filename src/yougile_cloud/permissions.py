"""What a company admin can switch off, and the Workflow chains they type in.

Restrictions are groups of core actions (``tasks.delete``, ``chats.send_message``...) with a
human label. They are stored as explicit action names, which the core's ``deny`` accepts.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from functools import cache

from yougile_mcp import catalog
from yougile_mcp.directory import Ambiguous, NotFound, Structure
from yougile_mcp.policy import action_names


@dataclass(frozen=True)
class Restriction:
    key: str
    label: str
    actions: tuple[str, ...]


def _writes(tool: str) -> list[str]:
    """Every non-read action of a tool, including ``<tool>.delete*`` via ``deleted: true``."""
    names: list[str] = []
    for op in catalog.by_tool()[tool].values():
        if op.access != "read":
            names.extend(n for n in action_names(op, {"deleted": True}) if n not in names)
    return names


@cache
def restrictions() -> tuple[Restriction, ...]:
    def r(key: str, label: str, *actions: str) -> Restriction:
        return Restriction(key, label, tuple(actions))

    return (
        r("task_create", "Создавать задачи", "tasks.create"),
        r("task_update", "Менять задачи: перенос, сроки, исполнители, часы", "tasks.update"),
        r("task_delete", "Удалять задачи", "tasks.delete"),
        r("chat_send", "Писать в чаты", "chats.send_message", "chats.typing"),
        r(
            "chat_edit",
            "Править и удалять сообщения",
            "chats.update_message",
            "chats.delete_message",
        ),
        r(
            "chat_members",
            "Менять участников чатов, заводить групповые чаты",
            "tasks.set_chat_subscribers",
            *[a for a in _writes("chats") if "group_chat" in a],
        ),
        r("files", "Загружать файлы", *_writes("files")),
        r("crm", "Создавать контакты в CRM", *_writes("crm")),
        r(
            "structure",
            "Менять проекты, роли в проектах, доски и колонки",
            *_writes("projects"),
            *_writes("boards"),
            *_writes("columns"),
        ),
        r("stickers", "Менять стикеры", *_writes("stickers")),
        r("people", "Приглашать, менять и удалять сотрудников и отделы", *_writes("users")),
        r("company", "Менять данные компании и вебхуки", *_writes("company")),
    )


def all_write_actions() -> set[str]:
    return {a for tool in catalog.TOOLS for a in _writes(tool)}


def denied_keys(patterns: list[str]) -> set[str]:
    """Restrictions fully covered by stored deny patterns (masks like ``users.*`` count too)."""
    clean = [p.removeprefix("yougile_") for p in patterns]
    return {
        r.key
        for r in restrictions()
        if all(any(fnmatch.fnmatchcase(a, p) for p in clean) for a in r.actions)
    }


def deny_list(keys: list[str] | set[str]) -> list[str]:
    """Action names for the chosen restriction keys; unknown keys are ignored."""
    chosen = set(keys)
    return [a for r in restrictions() if r.key in chosen for a in r.actions]


# ---------- Workflow chains ----------

_ARROW = re.compile(r"\s*(?:→|->|⟶)\s*")


def format_workflows(workflows: dict[str, list[str]]) -> str:
    return "\n".join(f"{board}: {' → '.join(chain)}" for board, chain in workflows.items())


def parse_workflows(text: str, structure: Structure) -> tuple[dict[str, list[str]], list[str]]:
    """``Проект / Доска: Колонка → Колонка`` per line, checked against the real boards.

    Returns the chains keyed by the canonical "Project / Board" label with the columns' real
    titles, and human-readable errors (one per bad line).
    """
    workflows: dict[str, list[str]] = {}
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        board_ref, sep, rest = line.partition(":")
        titles = [t for t in _ARROW.split(rest.strip()) if t] if sep else []
        if not board_ref.strip() or len(titles) < 2:
            errors.append(
                f"Строка {number}: нужен вид «Проект / Доска: Колонка → Колонка», "
                "в цепочке хотя бы две колонки."
            )
            continue
        try:
            board = structure.find_board(board_ref.strip())
        except NotFound, Ambiguous:
            errors.append(f"Строка {number}: доска «{board_ref.strip()}» не найдена или не одна.")
            continue
        label = structure.board_label(board)
        if label in workflows:
            errors.append(f"Строка {number}: доска «{label}» уже описана выше.")
            continue
        chain: list[str] = []
        for title in titles:
            try:
                chain.append(structure.find_column(title, board["id"]).get("title", title))
            except NotFound, Ambiguous:
                errors.append(f"Строка {number}: на доске «{label}» нет колонки «{title}».")
                break
        else:
            workflows[label] = chain
    return workflows, errors
