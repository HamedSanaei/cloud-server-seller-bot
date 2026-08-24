"""Terms acceptance versioning service (M02-004).

Terms are immutable, strictly increasing versions. Publishing a new version
makes it "the latest" immediately; users whose accepted version lags behind
must re-accept before actions that require it (provisioning).

- Publishing is admin-gated (``admin:manage_settings``) and audited.
- Acceptance is idempotent: accepting while already current is a no-op.
- ``require_latest`` is the provisioning gate: it raises
  :class:`TermsAcceptanceRequiredError` for stale users and passes through
  when no terms have been published (nothing to accept).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.users.domain import (
    Permission,
    PermissionChecker,
    TermsAcceptanceRequiredError,
    TermsVersion,
    TermsVersionRepository,
    User,
    UserRepository,
)


class TermsService:
    """Publish/accept terms versions and gate actions on the latest version."""

    def __init__(
        self,
        terms_repo: TermsVersionRepository,
        user_repo: UserRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._terms = terms_repo
        self._users = user_repo
        self._audit = AuditTrail(audit_repo)

    async def get_latest(self) -> TermsVersion | None:
        return await self._terms.get_latest()

    async def publish(
        self,
        *,
        admin: User | None,
        summary: str,
        body: str,
    ) -> TermsVersion:
        """Publish the next terms version (admin-only, audited)."""
        if admin is not None:
            PermissionChecker(admin).require(Permission.ADMIN_MANAGE_SETTINGS)
        latest = await self._terms.get_latest()
        version = (latest.version + 1) if latest is not None else 1
        terms = TermsVersion(
            version=version,
            body=body.strip(),
            effective_at=datetime.now(UTC),
            summary=summary.strip(),
        )
        published = await self._terms.publish(terms)
        actor_type = ActorType.ADMIN if admin is not None else ActorType.SYSTEM
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=admin.id if admin is not None else None,
            action="terms.publish",
            resource_type="terms",
            resource_id=str(version),
            reason=summary.strip() or f"published terms version {version}",
            metadata={"version": str(version)},
        )
        return published

    async def user_requires_acceptance(self, user: User) -> bool:
        """True when ``user`` must (re-)accept the latest terms."""
        latest = await self._terms.get_latest()
        return user.needs_terms_acceptance(latest.version if latest else None)

    async def require_latest(self, user: User) -> None:
        """Provisioning gate: raise unless the user accepted the latest terms."""
        latest = await self._terms.get_latest()
        if latest is None:
            return  # nothing published yet: nothing to accept
        if user.needs_terms_acceptance(latest.version):
            raise TermsAcceptanceRequiredError(
                f"user {user.id} accepted terms "
                f"{user.terms_version if user.terms_version is not None else 'never'}; "
                f"latest is {latest.version}"
            )

    async def accept(self, *, user_id: UUID) -> User:
        """Record that ``user_id`` accepted the current terms (idempotent).

        With no published terms this is a no-op (returns the user unchanged).
        """
        latest = await self._terms.get_latest()
        user = await self._users.get(user_id)
        if user is None:
            raise LookupError(f"user {user_id} not found")
        if latest is None or not user.needs_terms_acceptance(latest.version):
            return user
        user.accept_terms(latest.version)
        updated = await self._users.update_terms(user_id, latest.version)
        actor_type = ActorType.ADMIN if user.role.value == "admin" else ActorType.USER
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=user_id,
            action="terms.accept",
            resource_type="user",
            resource_id=str(user_id),
            reason=f"accepted terms version {latest.version}",
            metadata={"version": str(latest.version)},
        )
        return updated
