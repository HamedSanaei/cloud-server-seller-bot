"""Shared helpers for the server-rendered web panels (M14-004/M14-005).

The panels are a deliberately SMALL shell on top of the same
application-layer services the REST v1 surface uses:

- **Session** = HttpOnly cookie holding the customer's own API token
  (``cpt_...``). No new credential kind is invented; validation goes
  through the SAME hashed-token authenticator as bearer auth, so a
  logout/revocation kills web sessions immediately.
- Views render dependency-free escaped HTML (stdlib ``html.escape``).
"""

from __future__ import annotations

from html import escape as _e

from fastapi import Request, Response

SESSION_COOKIE = "platform_session"

_PAGE_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Cloud Platform</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a2e}}
table{{border-collapse:collapse;margin:1rem 0}}
td,th{{border:1px solid #ccc;padding:.35rem .7rem;text-align:left}}
th{{background:#eef}}
.error{{color:#b00020}}
.muted{{color:#666}}
form{{margin:1rem 0}}
input{{padding:.4rem;margin:.2rem 0;width:24rem}}
button{{padding:.45rem 1rem}}
</style></head><body>
<h1>{title}</h1>
{body}
</body></html>"""


def page(title: str, body: str) -> str:
    """A minimal escaped HTML page shell."""
    return _PAGE_SHELL.format(title=_e(title), body=body)


def esc(value: object) -> str:
    """Escape one value for safe interpolation into HTML."""
    return _e(str(value))


async def session_token(request: Request) -> str | None:
    """The raw API token from the session cookie, if present."""
    return request.cookies.get(SESSION_COOKIE)


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)
