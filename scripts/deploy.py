"""Deploy an immutable image through the same guarded engine used by GitHub Actions.

Both release lanes update the existing production path, database, Redis, private
TOML and single Telegram bot. Staging skips only the one-shot catalog refresh.
There is no rolling/blue-green executor that can bypass backup, writer draining
or rollback guards. --dry-run describes the release without changing files.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_RE = re.compile(r"ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+:([0-9a-f]{40})\Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="ghcr.io/owner/repo:<full-commit-sha>")
    parser.add_argument(
        "--environment",
        required=True,
        choices=("staging", "production"),
        help="release lane; both lanes update the same production runtime",
    )
    parser.add_argument("--deploy-path", required=True, type=Path)
    parser.add_argument("--configuration-path", required=True, type=Path)
    parser.add_argument("--confirm-production", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    match = IMAGE_RE.fullmatch(args.image)
    if match is None:
        parser.error("image must use an immutable full 40-character commit SHA")
    if args.environment == "production" and not args.confirm_production:
        parser.error("production requires separate explicit --confirm-production approval")
    if not args.deploy_path.is_absolute() or not args.configuration_path.is_absolute():
        parser.error("deployment and private configuration paths must be absolute")
    compose = ROOT / "deploy" / "production" / "docker-compose.yml"
    content = compose.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    sha = match.group(1)
    candidate = args.deploy_path / f".docker-compose.{sha}.candidate.yml"
    if args.dry_run:
        print(
            f"Release lane: {args.environment}; "
            "runtime/project: production/cloud-platform-production"
        )
        print(f"Release: {sha}; image: {args.image}; compose SHA-256: {digest}")
        print(f"Target: {args.deploy_path}; server-owned configuration: {args.configuration_path}")
        print(
            "Engine: deploy-production.sh (lock, preflight, drain, encrypted backup, "
            "migration, health, safe rollback)"
        )
        return 0
    if os.name != "posix":
        parser.error("execute on the authorized Linux deployment target")
    if not args.deploy_path.is_dir() or not args.configuration_path.is_file():
        parser.error("the shared production target and its private configuration must exist")
    # The manual path must not pair an approved image with uncommitted or
    # different-branch release scripts. CI performs its own exact-SHA CI proof.
    branch = "main" if args.environment == "production" else "staging"
    try:
        remote = (
            subprocess.run(
                ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
                cwd=ROOT,
                capture_output=True,
                check=True,
                timeout=30,
            )
            .stdout.decode()
            .split()
        )
        if not remote or remote[0] != sha:
            parser.error("image is not the current commit of the selected remote branch")
        for relative in ("deploy/production/docker-compose.yml", "scripts/deploy-production.sh"):
            approved = subprocess.run(
                ["git", "show", f"{sha}:{relative}"],
                cwd=ROOT,
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout
            if (ROOT / relative).read_bytes() != approved:
                parser.error("release-owned files differ from the selected immutable commit")
    except (OSError, subprocess.SubprocessError):
        parser.error(
            "cannot verify the approved branch/commit; fetch it or use "
            "the protected GitHub workflow"
        )
    environment = os.environ.copy()
    environment.pop("DEPLOY_PRODUCTION_SOURCED", None)
    environment.update(
        DEPLOY_PROFILE=args.environment,
        DEPLOY_PATH=str(args.deploy_path),
        PLATFORM_IMAGE_NEW=args.image,
        CONFIGURATION_PATH=str(args.configuration_path),
        CANDIDATE_COMPOSE_FILE=str(candidate),
        CURRENT_COMPOSE_FILE=str(args.deploy_path / "docker-compose.yml"),
        ENV_FILE=str(args.deploy_path / "deploy.env"),
        EXPECTED_COMPOSE_SHA256=digest,
    )
    # Never overwrite a candidate belonging to another active deployment.
    try:
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("release candidate already exists; inspect the active deployment first")
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
    try:
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "deploy-production.sh")],
            env=environment,
            check=False,
        ).returncode
    finally:
        candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
