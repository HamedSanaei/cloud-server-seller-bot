"""Customer web panel shell (M14-004).

Acceptance: login/session and server/wallet views.

- **Login/session**: the panel does NOT invent a new credential. The
  customer pastes one of their OWN API tokens (``cpt_...``); it is
  validated through the same hashed-token authenticator as bearer auth
  and then carried as an HttpOnly session cookie. Revoking the token
  kills the web session on its next request (authentication re-runs per
  request; unknown/revoked tokens raise, which we treat as anonymous).
- **Server view**: the ownership-scoped server list.
- **Wallet view**: current balance in minor units with a major-unit hint.

All rendering is escaped HTML; no client-side framework, no inline event
handlers - a shell to grow into, not a product surface yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .common import clear_session_cookie, esc, page, session_token, set_session_cookie

router = APIRouter(prefix="/web", include_in_schema=False)


class WebIdentity:
    """Resolved session identity for one request."""

    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id


TokenAuthenticator = Callable[[str], Awaitable[Any]]


async def _authenticator() -> TokenAuthenticator:
    """The platform token authenticator (dependency-overridable)."""
    from cloud_platform.core.container import get_container

    container = await get_container()
    return container.token_service().authenticate


async def _optional_identity(
    request: Request,
    authenticate: Annotated[TokenAuthenticator, Depends(_authenticator)],
) -> WebIdentity | None:
    """Resolve the session cookie to an identity; None when anonymous."""
    raw = await session_token(request)
    if not raw:
        return None
    try:
        auth = await authenticate(raw)
    except Exception:
        return None
    if auth is None:
        return None
    return WebIdentity(auth.user_id)


def _login_page(error: str | None = None) -> HTMLResponse:
    body = """
<p class="muted">Sign in with one of your API tokens (starts with <code>cpt_</code>).
Tokens are validated against their stored hash only and can be revoked any time.</p>
<form method="post" action="/web/login">
  <label for="token">API token</label><br>
  <input type="password" id="token" name="token" autocomplete="off" autofocus>
  <br><button type="submit">Sign in</button>
</form>"""
    if error:
        body = f'<p class="error">{esc(error)}</p>' + body
    return HTMLResponse(page("Sign in", body))


@router.get("/login", response_class=HTMLResponse)
async def login_form() -> HTMLResponse:
    return _login_page()


@router.post("/login")
async def login_submit(
    token: Annotated[str, Form()],
    authenticate: Annotated[TokenAuthenticator, Depends(_authenticator)],
) -> Response:
    """Validate the presented token and start a cookie session."""
    try:
        auth = await authenticate(token.strip())
    except Exception:
        auth = None
    if auth is None:
        return _login_page("That token was not accepted (unknown or revoked).")
    response = RedirectResponse("/web/panel", status_code=303)
    set_session_cookie(response, token.strip())
    return response


@router.get("/logout")
async def logout() -> Response:
    response = RedirectResponse("/web/login", status_code=303)
    clear_session_cookie(response)
    return response


async def _servers_view() -> Any:
    from cloud_platform.core.container import get_container
    from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
    from cloud_platform.modules.compute.service import MyServersService

    container = await get_container()
    return MyServersService(SqlAlchemyServerRepository(container.session_factory))


WalletLoader = Callable[[UUID], Awaitable[Any]]


async def _wallet_view() -> WalletLoader:
    """A loader returning the user's wallet or None (overridable)."""
    from cloud_platform.core.container import get_container
    from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

    container = await get_container()

    async def _load(user_id: UUID) -> Any:
        wallets = SqlAlchemyWalletRepository(container.session_factory)
        return await wallets.get(user_id)

    return _load


@router.get("/panel", response_class=HTMLResponse)
async def panel(
    identity: Annotated[WebIdentity | None, Depends(_optional_identity)],
    servers_service: Annotated[Any, Depends(_servers_view)],
    wallet_loader: Annotated[WalletLoader, Depends(_wallet_view)],
) -> Response:
    if identity is None:
        return RedirectResponse("/web/login", status_code=303)

    page_data = await servers_service.list_servers(identity.user_id)
    rows = "".join(
        f"<tr><td>{esc(s.server_id)}</td><td>{esc(s.provider_key)}</td><td>{esc(s.state)}</td></tr>"
        for s in page_data.items
    )
    server_table = (
        "<table><tr><th>Server</th><th>Provider</th><th>State</th></tr>"
        + (rows or '<tr><td colspan="3" class="muted">No servers yet</td></tr>')
        + "</table>"
    )

    wallet = await wallet_loader(identity.user_id)
    if wallet is None:
        wallet_html = '<p class="muted">No wallet yet.</p>'
    else:
        balance = int(wallet.balance)
        wallet_html = (
            f"<p>Balance: <strong>{balance}</strong> minor "
            f"({balance / 100:.2f} {esc(wallet.currency)})</p>"
        )

    body = (
        f'<p class="muted">Signed in as <code>{esc(identity.user_id)}</code> '
        f'- <a href="/web/logout">sign out</a></p>'
        f"<h2>Your servers</h2>{server_table}"
        f"<h2>Wallet</h2>{wallet_html}"
    )
    return HTMLResponse(page("Panel", body))
