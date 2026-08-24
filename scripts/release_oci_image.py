"""Build (and optionally publish) the platform OCI image (M12-002).

Acceptance: a **tagged immutable image** is produced.

The image is tagged with the FULL git commit SHA by default - an immutable
tag: the same tag is never re-pushed, and every registry blob is addressable
by its content digest. Mutable tags (``latest`` or version tags) are only
ever produced by an explicit ``--tag`` flag, so a plain release run can
never overwrite a published image.

Usage:
    uv run python scripts/release_oci_image.py                # build :<sha>
    uv run python scripts/release_oci_image.py --push         # + publish
    uv run python scripts/release_oci_image.py --tag v1.0.0   # extra tag
    uv run python scripts/release_oci_image.py --image my.reg/platform --push

The build requires a docker daemon. ``--allow-dirty`` disables the clean-tree
check (the tag still names a commit; the tree may contain uncommitted edits,
which makes the tag no longer reproducible - use only for local smoke tests).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_IMAGE = "cloud-server-platform"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class ReleaseError(Exception):
    """The image could not be built or published."""


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    """What will be built and published, decided BEFORE any side effect."""

    image: str
    commit: str
    tags: list[str] = field(default_factory=list)
    push: bool = False

    @property
    def immutable_tag(self) -> str:
        return f"{self.image}:{self.commit}"

    def __str__(self) -> str:
        extra = f", extra tags: {', '.join(self.tags)}" if self.tags else ""
        action = "build+push" if self.push else "build"
        return f"{action} {self.image} @ {self.commit} ({self.commit[:12]}){extra}"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    # Capture bytes and decode ourselves: docker's progress output is UTF-8
    # (box-drawing characters) and must never crash the release on a locale
    # encoding that cannot decode it (e.g. cp1252 on Windows).
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True)
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")
    if stdout.strip():
        print(stdout.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        if stderr.strip():
            print(stderr.rstrip(), file=sys.stderr)
        raise ReleaseError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr=stderr)


def git_commit() -> str:
    result = run(["git", "rev-parse", "HEAD"])
    commit = result.stdout.strip()
    if not FULL_SHA.match(commit):
        raise ReleaseError("not inside a git repository (or git failed); cannot tag an image")
    return commit


def tree_dirty() -> bool:
    result = run(["git", "status", "--porcelain"])
    return bool(result.stdout.strip())


def plan_release(
    image: str,
    commit: str,
    extra_tags: list[str] | None = None,
    push: bool = False,
) -> ReleasePlan:
    tags = [f"{image}:{extra}" for extra in (extra_tags or [])]
    return ReleasePlan(image=image, commit=commit, tags=tags, push=push)


def build(plan: ReleasePlan) -> None:
    run(["docker", "build", "--pull", "-t", plan.immutable_tag, "."])


def verify_image(plan: ReleasePlan) -> str:
    """The image must exist locally; returns its content digest."""
    result = run(["docker", "image", "inspect", "--format", "{{.Id}}", plan.immutable_tag])
    digest = result.stdout.strip()
    if not digest:
        raise ReleaseError(f"image {plan.immutable_tag} not found after build")
    return digest


def publish(plan: ReleasePlan) -> None:
    for tag in [plan.immutable_tag, *plan.tags]:
        run(["docker", "push", tag])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_oci_image")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="registry/repository name")
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="extra tag for this commit (repeatable); mutable tags are ONLY produced this way",
    )
    parser.add_argument("--push", action="store_true", help="publish the image(s) after building")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build even with uncommitted changes (tag is no longer reproducible)",
    )
    args = parser.parse_args(argv)

    for extra in args.tag:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", extra):
            print(f"release failed: invalid tag {extra!r}", file=sys.stderr)
            return 1

    commit = git_commit()
    if not args.allow_dirty and tree_dirty():
        print(
            "release failed: working tree is dirty - commit first so the "
            "immutable tag actually names what was built (or use --allow-dirty)",
            file=sys.stderr,
        )
        return 1

    plan = plan_release(args.image, commit, args.tag, args.push)
    print(f"release plan: {plan}")
    try:
        build(plan)
        digest = verify_image(plan)
        if plan.push:
            publish(plan)
    except ReleaseError as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 1
    print(f"image ready: {plan.immutable_tag} (digest {digest})")
    for extra_tag in plan.tags:
        print(f"extra tag:  {extra_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
