"""HTML of the company admin pages. Every interpolated value goes through ``e()``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from urllib.parse import quote
from zoneinfo import ZoneInfo, available_timezones

from .access import Access
from .db import Company, Rights, User
from .pages import e, page
from .permissions import denied_keys, restrictions

ROLES = {
    "reader": "Чтение — только смотреть",
    "member": "Участник — задачи, чаты, файлы",
    "admin": "Администратор — ещё проекты, доски, сотрудники, стикеры",
}
SHORT_ROLE = {"reader": "чтение", "member": "участник", "admin": "администратор"}
DONE = {
    "settings": "Настройки сохранены. Подключения подхватят их в течение минуты.",
    "rights": "Права сотрудника сохранены.",
    "reset": "Личные права сброшены: действуют настройки компании.",
    "disconnect": "Сотрудник отключён от YouGile MCP.",
}


@cache
def timezones() -> frozenset[str]:
    return frozenset(available_timezones())


@dataclass(frozen=True)
class Frame:
    """What every admin page shows around its content."""

    company: Company
    access: Access
    admin: User
    csrf: str
    tz: ZoneInfo
    default_role: str
    company_denied: set[str]


@dataclass(frozen=True)
class Person:
    id: str  # YouGile user id
    name: str
    email: str
    yougile_admin: bool
    in_yougile: bool
    user: User | None  # has a key on this server (signed in at least once)
    rights: Rights | None


@dataclass
class SettingsForm:
    timezone: str
    default_role: str
    instructions: str
    confirm: set[str] = field(default_factory=set)
    deny: set[str] = field(default_factory=set)
    workflows: str = ""


@dataclass
class RightsForm:
    role: str = ""  # "" = the company default
    scope: str = "all"  # all | some
    projects: set[str] = field(default_factory=set)
    deny: set[str] = field(default_factory=set)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _status(access: Access, tz: ZoneInfo) -> str:
    until = f" до {access.until.astimezone(tz):%d.%m.%Y}" if access.until else ""
    return {
        "exempt": "Бесплатный доступ",
        "active": "Оплачено" + until,
        "trial": "Пробный период" + until,
    }.get(access.state, access.message())


def _csrf(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(token)}">'


def _errors(errors: list[str]) -> str:
    if not errors:
        return ""
    items = "".join(f"<li>{e(err)}</li>" for err in errors)
    return f'<div class="error" role="alert"><ul>{items}</ul></div>'


def _shell(frame: Frame, current: str, title: str, body: str, done: str | None) -> str:
    links = (("users", "/admin", "Сотрудники"), ("settings", "/admin/settings", "Настройки"))
    nav = "".join(
        f'<a href="{href}"{" aria-current=page" if key == current else ""}>{e(label)}</a>'
        for key, href, label in links
    )
    notice = f'<div class="ok" role="status">{e(DONE[done])}</div>' if done in DONE else ""
    who = frame.admin.name or frame.admin.email
    return page(
        title,
        f'<div class="top"><div><h1>{e(frame.company.name)}</h1>'
        f'<p class="lead">YouGile MCP · {e(_status(frame.access, frame.tz))} · {e(who)}</p></div>'
        f'<form method="post" action="/admin/logout">{_csrf(frame.csrf)}'
        '<button class="inline secondary" type="submit">Выйти</button></form></div>'
        f"<nav>{nav}</nav>{notice}{body}",
        wide=True,
    )


def _checkbox(name: str, value: str, label: str, checked: bool) -> str:
    return (
        f'<label><input type="checkbox" name="{e(name)}" value="{e(value)}"'
        f"{' checked' if checked else ''}> {e(label)}</label>"
    )


def _project_boxes(name: str, projects: list[tuple[str, str]], checked: set[str]) -> str:
    if not projects:
        return '<p class="muted">В компании пока нет проектов.</p>'
    boxes = "".join(_checkbox(name, pid, title, pid in checked) for pid, title in projects)
    return f'<div class="list">{boxes}</div>'


def _restriction_boxes(checked: set[str], locked: frozenset[str] | set[str] = frozenset()) -> str:
    rows = []
    for r in restrictions():
        if r.key in locked:
            rows.append(
                f'<label><input type="checkbox" checked disabled> {e(r.label)}'
                ' <span class="muted">— запрещено всей компании</span></label>'
            )
        else:
            rows.append(_checkbox("deny", r.key, r.label, r.key in checked))
    return f'<div class="list">{"".join(rows)}</div>'


# ---------- employees ----------


def _href(person: Person) -> str:
    return e(f"/admin/users/{quote(person.id, safe='')}")


def _when(moment: datetime | None, tz: ZoneInfo) -> str:
    return f"{moment.astimezone(tz):%d.%m.%Y %H:%M}" if moment else ""


def _mcp_cell(person: Person, tz: ZoneInfo) -> str:
    if person.user is None:
        return '<span class="muted">нет подключения</span>'
    seen = _when(person.user.last_seen_at, tz)
    return "подключено<br>" + (
        f'<span class="muted">последний запрос {e(seen)}</span>'
        if seen
        else '<span class="muted">запросов ещё не было</span>'
    )


def _role_cell(rights: Rights | None, default_role: str) -> str:
    if rights and rights.role:
        return e(SHORT_ROLE[rights.role])
    return f'{e(SHORT_ROLE[default_role])} <span class="muted">(по умолчанию)</span>'


def _projects_cell(rights: Rights | None, projects: dict[str, str]) -> str:
    if not rights or rights.projects is None:
        return "все"
    names = [projects.get(ref) or ref for ref in rights.projects]
    if not names:
        return '<span class="muted">ни одного</span>'
    shown = ", ".join(names[:3])
    return e(shown + (f" и ещё {len(names) - 3}" if len(names) > 3 else ""))


def _deny_cell(rights: Rights | None, company_denied: set[str]) -> str:
    total = len(company_denied | denied_keys(rights.deny if rights else []))
    return f"{total} {plural(total, 'запрет', 'запрета', 'запретов')}" if total else "—"


def users_page(
    frame: Frame, people: list[Person], projects: dict[str, str], done: str | None
) -> str:
    rows = []
    for p in people:
        pills = ('<span class="pill">админ YouGile</span>' if p.yougile_admin else "") + (
            "" if p.in_yougile else '<span class="pill">нет в YouGile</span>'
        )
        rows.append(
            f'<tr><td><a href="{_href(p)}"><b>{e(p.name)}</b></a>'
            f'<div class="muted">{e(p.email)}</div>{pills}</td>'
            f"<td>{_mcp_cell(p, frame.tz)}</td>"
            f'<td data-label="Роль">{_role_cell(p.rights, frame.default_role)}</td>'
            f'<td data-label="Проекты">{_projects_cell(p.rights, projects)}</td>'
            f'<td data-label="Запреты">{_deny_cell(p.rights, frame.company_denied)}</td>'
            f'<td><a href="{_href(p)}">Настроить</a></td></tr>'
        )
    table = (
        "<table><thead><tr><th>Сотрудник</th><th>YouGile MCP</th><th>Роль</th><th>Проекты</th>"
        f"<th>Запреты</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        if rows
        else '<p class="muted">Сотрудников не видно: у вашего ключа нет доступа к списку.</p>'
    )
    return _shell(
        frame,
        "users",
        "Сотрудники",
        '<p class="hint">Права в YouGile MCP только сужают права человека в самом YouGile: '
        "ассистент не сделает того, чего сотрудник не может сам. Права можно задать заранее, "
        "до подключения.</p>" + table,
        done,
    )


# ---------- company settings ----------


def settings_page(
    frame: Frame,
    values: SettingsForm,
    projects: list[tuple[str, str]],
    *,
    errors: list[str] | None = None,
    done: str | None = None,
) -> str:
    zones = "".join(f'<option value="{e(z)}">' for z in sorted(timezones()))
    roles = "".join(
        f'<option value="{e(k)}"{" selected" if k == values.default_role else ""}>{e(v)}</option>'
        for k, v in ROLES.items()
    )
    body = (
        _errors(errors or []) + f'<form method="post" action="/admin/settings">{_csrf(frame.csrf)}'
        '<label for="default_role">Роль по умолчанию</label>'
        f'<select id="default_role" name="default_role">{roles}</select>'
        '<p class="hint">Для всех, кому не заданы личные права на странице сотрудника.</p>'
        '<label for="instructions">Правила компании для ассистента</label>'
        f'<textarea class="prose" id="instructions" name="instructions" maxlength="4000">'
        f"{e(values.instructions)}</textarea>"
        '<p class="hint">Ассистент видит их у каждого сотрудника: как называть задачи, куда '
        "ставить новые, что делать нельзя.</p>"
        "<fieldset><legend>Спрашивать подтверждение перед записью в проекты</legend>"
        + _project_boxes("confirm", projects, values.confirm)
        + '<p class="hint">Ассистент попросит человека подтвердить каждое изменение в этих '
        "проектах — например, в проектах с клиентами.</p></fieldset>"
        "<fieldset><legend>Запрещено всей компании</legend>"
        + _restriction_boxes(values.deny)
        + "</fieldset>"
        '<label for="workflows">Цепочки Workflow</label>'
        f'<textarea id="workflows" name="workflows" placeholder="Клиенты / Сайт: Очередь → '
        f'В работе → На проверке → Готово">{e(values.workflows)}</textarea>'
        '<p class="hint">YouGile не отдаёт настройки Workflow по API. Одна доска — одна строка: '
        "«Проект / Доска: Колонка → Колонка → …». Перенося задачу, ассистент пройдёт все "
        "промежуточные колонки; первая колонка — место для новых задач.</p>"
        '<label for="timezone">Часовой пояс</label>'
        f'<input type="text" id="timezone" name="timezone" list="zones" '
        f'value="{e(values.timezone)}" autocomplete="off"><datalist id="zones">{zones}</datalist>'
        '<p class="hint">Для сроков задач и дат без времени. Например, Europe/Moscow.</p>'
        '<button type="submit">Сохранить</button></form>'
    )
    return _shell(frame, "settings", "Настройки компании", body, done)


# ---------- one employee ----------


def user_page(
    frame: Frame,
    person: Person,
    values: RightsForm,
    projects: list[tuple[str, str]],
    *,
    errors: list[str] | None = None,
    done: str | None = None,
) -> str:
    action = _href(person)
    default = SHORT_ROLE[frame.default_role]
    roles = f'<option value="">Как у компании ({e(default)})</option>' + "".join(
        f'<option value="{e(k)}"{" selected" if k == values.role else ""}>{e(v)}</option>'
        for k, v in ROLES.items()
    )
    scope = "".join(
        f'<label><input type="radio" name="scope" value="{key}"'
        f"{' checked' if values.scope == key else ''}> {label}</label>"
        for key, label in (("all", "Все проекты"), ("some", "Только отмеченные"))
    )
    pills = ('<span class="pill">админ YouGile</span>' if person.yougile_admin else "") + (
        "" if person.in_yougile else '<span class="pill">нет в YouGile</span>'
    )
    connection = (
        f"<p>Подключение к YouGile MCP: {_mcp_cell(person, frame.tz)}</p>"
        if person.user
        else '<p class="muted">Подключения к YouGile MCP ещё не было. Права начнут действовать '
        "с первого подключения.</p>"
    )
    disconnect = (
        f'<form method="post" action="{action}/disconnect">{_csrf(frame.csrf)}'
        '<button class="inline danger" type="submit">Отключить от YouGile MCP</button></form>'
        if person.user
        else ""
    )
    body = (
        f'<h2>{e(person.name)}</h2><p class="muted">{e(person.email)}</p>{pills}{connection}'
        + _errors(errors or [])
        + f'<form method="post" action="{action}">{_csrf(frame.csrf)}'
        '<label for="role">Роль</label>'
        f'<select id="role" name="role">{roles}</select>'
        f'<fieldset><legend>Проекты</legend><div class="list">{scope}</div>'
        + _project_boxes("project", projects, values.projects)
        + "</fieldset><fieldset><legend>Запреты</legend>"
        + _restriction_boxes(values.deny, frame.company_denied)
        + '</fieldset><button type="submit">Сохранить</button></form>'
        '<div class="actions">'
        f'<form method="post" action="{action}/reset">{_csrf(frame.csrf)}'
        '<button class="inline secondary" type="submit">Сбросить к настройкам компании</button>'
        f"</form>{disconnect}</div>"
        + (
            '<p class="hint">Отключение удаляет ключ сервера и выводит человека из всех '
            "AI-клиентов. Своим логином YouGile человек может подключиться снова; чтобы закрыть "
            "доступ совсем, уберите сотрудника из компании в YouGile.</p>"
            if person.user
            else ""
        )
    )
    return _shell(frame, "users", person.name, body, done)
