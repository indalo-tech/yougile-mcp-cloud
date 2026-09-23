"""Server-rendered HTML. Every interpolated value goes through ``e()``: no raw input in HTML."""

from __future__ import annotations

from html import escape

# AGPL: people using the service can always reach the code that runs it.
SOURCE = "https://github.com/indalo-tech/yougile-mcp-cloud"

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#5d6675;--line:#dfe3ea;--accent:#2f6fed;
--accent-text:#fff;--error-bg:#fdecec;--error:#a4262c;--ok-bg:#e8f5ec;--ok:#1d6b36}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--text:#e7eaf0;--muted:#a3abb9;
--line:#303642;--accent:#5b8cff;--accent-text:#0d1017;--error-bg:#3a1d1f;--error:#ff9aa0;
--ok-bg:#16301f;--ok:#8fdca9}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:420px;margin:8vh auto;padding:0 16px}main.wide{max-width:960px;margin-top:4vh}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:22px;margin:0 0 6px}p.lead{color:var(--muted);margin:0 0 20px}
label{display:block;font-weight:600;margin:14px 0 6px}
input[type=text],input[type=email],input[type=password],select,textarea{width:100%;
padding:10px 12px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
textarea{min-height:110px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px}
button{margin-top:20px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);
color:var(--accent-text);font:inherit;font-weight:600;cursor:pointer}
button.inline{width:auto;padding:8px 16px;margin-top:12px}
.note{color:var(--muted);font-size:13px;margin-top:16px}
.error{background:var(--error-bg);color:var(--error);border-radius:8px;padding:10px 12px;
margin:12px 0}
.ok{background:var(--ok-bg);color:var(--ok);border-radius:8px;padding:10px 12px;margin:12px 0}
.choice{display:block;width:100%;text-align:left;margin-top:10px;background:var(--bg);
color:var(--text);border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.checks label{display:inline-flex;gap:6px;font-weight:400;margin:4px 14px 4px 0}
nav{display:flex;gap:16px;margin:4px 0 20px;flex-wrap:wrap}nav a{color:var(--accent);
text-decoration:none}nav a[aria-current]{color:var(--text);font-weight:600}
a{color:var(--accent)}h2{font-size:18px;margin:28px 0 8px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.top button{margin-top:0}.muted{color:var(--muted)}.hint{color:var(--muted);font-size:13px;
margin:4px 0 0}fieldset{border:1px solid var(--line);border-radius:8px;padding:8px 14px 12px;
margin:18px 0 0}legend{font-weight:600;padding:0 4px}
.list label{display:flex;gap:8px;align-items:baseline;font-weight:400;margin:6px 0}
.pill{display:inline-block;font-size:12px;padding:0 8px;border-radius:999px;margin:2px 6px 0 0;
border:1px solid var(--line);color:var(--muted)}
button.secondary{background:var(--bg);color:var(--text);border:1px solid var(--line)}
button.danger{background:var(--error-bg);color:var(--error)}
.actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:8px}
.actions form{margin:0}
textarea.prose{font-family:inherit;font-size:15px}p.foot{text-align:center;margin-top:20px}
@media (max-width:640px){table,thead,tbody,tr,td,th{display:block}thead{display:none}
td{border:0;padding:4px 0}tr{border-bottom:1px solid var(--line);padding:8px 0}
td[data-label]::before{content:attr(data-label) ": ";color:var(--muted)}}
"""


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, *, wide: bool = False) -> str:
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{e(title)}</title><style>{CSS}</style>'
        f'</head><body><main class="{"wide" if wide else ""}"><div class="card">{body}</div>'
        f'<p class="note foot">YouGile MCP · <a href="{SOURCE}">исходный код</a> (AGPL-3.0)</p>'
        "</main></body></html>"
    )


def login_page(
    *,
    action: str,
    hidden: dict[str, str],
    heading: str,
    lead: str,
    error: str | None = None,
    login: str = "",
) -> str:
    fields = "".join(
        f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in hidden.items()
    )
    return page(
        heading,
        f"<h1>{e(heading)}</h1><p class=lead>{e(lead)}</p>"
        + (f'<div class="error" role="alert">{e(error)}</div>' if error else "")
        + f'<form method="post" action="{e(action)}">{fields}'
        '<label for="login">Логин YouGile (email)</label>'
        '<input id="login" name="login" type="email" autocomplete="username" required '
        f'value="{e(login)}">'
        '<label for="password">Пароль YouGile</label>'
        '<input id="password" name="password" type="password" '
        'autocomplete="current-password" required>'
        '<button type="submit">Войти</button></form>'
        '<p class="note">Пароль передаётся в YouGile один раз, чтобы выпустить ключ доступа, '
        "и нигде не сохраняется. Ключ хранится в зашифрованном виде и действует с вашими "
        "правами в YouGile.</p>",
    )


def company_page(
    *,
    action: str,
    hidden: dict[str, str],
    companies: list[tuple[str, str]],
    lead: str = "Вы состоите в нескольких компаниях YouGile. "
    "Подключение будет работать с одной из них.",
) -> str:
    fields = "".join(
        f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in hidden.items()
    )
    buttons = "".join(
        f'<button class="choice" type="submit" name="company" value="{e(cid)}">{e(name)}</button>'
        for cid, name in companies
    )
    return page(
        "Выбор компании",
        f"<h1>Выберите компанию</h1><p class=lead>{e(lead)}</p>"
        f'<form method="post" action="{e(action)}">{fields}{buttons}</form>',
    )


def message_page(
    title: str, text: str, *, error: bool = True, link: tuple[str, str] | None = None
) -> str:
    kind = "error" if error else "ok"
    more = f'<p><a href="{e(link[0])}">{e(link[1])}</a></p>' if link else ""
    return page(title, f'<h1>{e(title)}</h1><div class="{kind}">{e(text)}</div>{more}')
