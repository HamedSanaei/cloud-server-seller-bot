"""Admin web panel shell (M14-005).

Acceptance: provider/users/operations dashboards.

Same session mechanism as the customer panel (an API-token cookie), but
every request additionally re-checks platform RBAC: the session's user
must exist, be ACTIVE and hold the admin settings permission. A frozen
or banned admin loses the panel immediately - the check is per request,
never cached. Anonymous and non-admin sessions are treated IDENTICALLY
(a redirect to login), so the panel never leaks whether an id exists.

Dashboards:
- **users** - search + status (reuses the M14-003 search repository);
- **operations** - the newest audit events (the operations feed);
- **providers** - registered provider adapters from the live registry.

Every data source is an overridable dependency so tests can drive the
views without a database.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from cloud_platform.modules.audit.domain import AuditEvent
from cloud_platform.modules.users.domain import Permission, Role, User
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository

from .common import esc, page, session_token

router = APIRouter(prefix="/web/admin", include_in_schema=False)

AuditFeedLoader = Callable[[int], Awaitable[list[AuditEvent]]]
ProvidersLoader = Callable[[], Awaitable[list[tuple[str, str]]]]


async def _user_repo() -> SqlAlchemyUserRepository:
    from cloud_platform.core.container import get_container

    container = await get_container()
    return SqlAlchemyUserRepository(container.session_factory)


async def _admin_identity(
    request: Request,
    repo: Annotated[SqlAlchemyUserRepository, Depends(_user_repo)],
) -> User | None:
    """The ACTIVE ADMIN behind this cookie, or None."""
    raw = await session_token(request)
    if not raw:
        return None
    authenticator = getattr(request.app.state, "authenticate_token", None)
    if authenticator is None:
        return None
    try:
        auth = await authenticator(raw)
    except Exception:
        return None
    user = await repo.get(auth.user_id)
    if user is None or user.role is not Role.ADMIN or not user.is_active:
        return None
    if not user.has_permission(Permission.ADMIN_MANAGE_SETTINGS):
        return None
    return user


async def _user_search() -> Callable[..., Awaitable[list[User]]]:
    """A loader for the user search (overridable)."""
    repo = await _user_repo()

    async def _search(q: str, *, offset: int = 0, limit: int = 20) -> list[User]:
        return await repo.search(q, offset=offset, limit=limit)

    return _search


async def _audit_feed() -> AuditFeedLoader:
    """A loader returning the newest audit events (overridable)."""
    from cloud_platform.core.container import get_container
    from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository

    container = await get_container()

    async def _load(limit: int) -> list[AuditEvent]:
        audit_repo = SqlAlchemyAuditRepository(container.session_factory)
        return await audit_repo.list_recent(limit=limit)

    return _load


async def _providers_view() -> ProvidersLoader:
    """A loader returning [(provider_key, capabilities)] (overridable)."""
    from cloud_platform.core.container import get_container

    container = await get_container()

    async def _load() -> list[tuple[str, str]]:
        registry = getattr(container, "provider_registry", None)
        out: list[tuple[str, str]] = []
        if registry is not None:
            for key in sorted(registry.keys()):
                try:
                    provider = registry.get(key)
                    caps = ", ".join(sorted(c.value for c in provider.capabilities))
                except Exception:
                    caps = "unavailable"
                out.append((key, caps))
        return out

    return _load


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    actor: Annotated[User | None, Depends(_admin_identity)],
    user_search: Annotated[Callable[..., Awaitable[list[User]]], Depends(_user_search)],
    audit_feed: Annotated[AuditFeedLoader, Depends(_audit_feed)],
    providers_view: Annotated[ProvidersLoader, Depends(_providers_view)],
    q: Annotated[str, Query()] = "",
) -> Response:
    if actor is None:
        # anonymous and non-admin are indistinguishable - no existence leak
        return RedirectResponse("/web/login", status_code=303)

    # -- users ------------------------------------------------------------
    users = await user_search(q, offset=0, limit=20)
    user_rows = "".join(
        f"<tr><td>{esc(u.username)}</td><td>{esc(u.email)}</td>"
        f"<td>{esc(u.role.value)}</td><td>{esc(u.status.value)}</td></tr>"
        for u in users
    )
    users_html = (
        "<form method='get' action='/web/admin'>"
        "<input type='text' name='q' placeholder='search username/email'></form>"
        "<table><tr><th>Username</th><th>Email</th><th>Role</th><th>Status</th></tr>"
        + (user_rows or '<tr><td colspan="4" class="muted">No matches</td></tr>')
        + "</table>"
    )

    # -- operations (audit feed) -------------------------------------------
    events = await audit_feed(15)
    event_rows = "".join(
        f"<tr><td>{esc(e.occurred_at)}</td><td>{esc(e.actor_type.value)}</td>"
        f"<td>{esc(e.action)}</td><td>{esc(e.resource_type)}</td>"
        f"<td>{esc(e.reason)}</td></tr>"
        for e in events
    )
    ops_html = (
        "<table><tr><th>When</th><th>Actor</th><th>Action</th><th>Resource</th>"
        "<th>Reason</th></tr>" + event_rows + "</table>"
    )

    # -- providers ----------------------------------------------------------
    providers = await providers_view()
    provider_rows = "".join(
        f"<tr><td>{esc(key)}</td><td>{esc(caps)}</td></tr>" for key, caps in providers
    )
    providers_html = (
        "<table><tr><th>Provider</th><th>Capabilities</th></tr>"
        + (provider_rows or '<tr><td colspan="2" class="muted">None registered</td></tr>')
        + "</table>"
    )

    body = (
        '<p class="muted">Admin: '
        f"<code>{esc(actor.username)}</code></p>"
        "<h2>Users</h2>"
        + users_html
        + "<h2>Recent operations</h2>"
        + ops_html
        + "<h2>Providers</h2>"
        + providers_html
        + '<p class="muted"><a href="/web/logout">sign out</a></p>'
    )
    return HTMLResponse(page("Admin", body))
