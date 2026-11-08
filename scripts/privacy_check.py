"""Privacy guard (Spec 11 §2.4): keep personal data out of the repository.

Two kinds of check:

* **Generic** (always on, safe for CI): home-directory paths with a real-looking
  user name, email addresses outside reserved example domains, and mail archive
  files (.pst/.ost/.mbox/.eml/.msg) committed to the tree.
* **Denylist** (local only): every line of ``.privacy-denylist`` is a
  case-insensitive term, or a regular expression when prefixed with ``re:``.
  The denylist must never be committed (it would publish the very terms it
  guards), so the check refuses to run if git tracks it.

Usage::

    uv run python scripts/privacy_check.py            # tracked + untracked, non-ignored files
    uv run python scripts/privacy_check.py --staged   # what is about to be committed
    git log --all -p | uv run python scripts/privacy_check.py --stdin
    uv run python scripts/privacy_check.py path/to/file ...

Exits 1 and prints ``file:line: [rule] match`` for every hit.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = ROOT / ".privacy-denylist"

ARCHIVE_EXTS = {".pst", ".ost", ".mbox", ".mbx", ".eml", ".msg", ".olm", ".dbx"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".cache", "data", ".pytest_cache", ".ruff_cache"}
TEXT_LIMIT = 5 * 1024 * 1024

# Placeholder user names that are fine in docs and tests
SAFE_USERS = {"user", "you", "me", "x", "example", "name", "username", "runner", "alice", "bob", "demo", "<name>", "…"}
HOME_RE = re.compile(r"(?<![\w.])/(?:home|Users)/([A-Za-z0-9._<>…-]+)")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
SAFE_DOMAIN_SUFFIXES = (
    "example.com", "example.org", "example.net", ".example", ".test", ".invalid", ".localhost",
    "users.noreply.github.com", "anthropic.com",
)
SAFE_DOMAINS = {"example.com", "example.org", "example.net", "localhost"}


def _safe_domain(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    return d in SAFE_DOMAINS or d.endswith(SAFE_DOMAIN_SUFFIXES) or d.endswith(tuple("." + s for s in SAFE_DOMAINS))


def load_denylist() -> list[tuple[str, re.Pattern]]:
    if not DENYLIST.exists():
        return []
    rules = []
    for raw in DENYLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            rules.append((line, re.compile(line[3:], re.IGNORECASE)))
        else:
            rules.append((line, re.compile(re.escape(line), re.IGNORECASE)))
    return rules


def check_text(name: str, text: str, deny: list[tuple[str, re.Pattern]]) -> Iterator[str]:
    for n, line in enumerate(text.splitlines(), start=1):
        for m in HOME_RE.finditer(line):
            if m.group(1).lower() not in SAFE_USERS:
                yield f"{name}:{n}: [home-path] {m.group(0)}"
        for m in EMAIL_RE.finditer(line):
            if not _safe_domain(m.group(1)):
                yield f"{name}:{n}: [email] {m.group(0)}"
        for _label, rx in deny:
            m = rx.search(line)
            if m:
                yield f"{name}:{n}: [denylist] {m.group(0)!r}"


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return [x for x in out.stdout.splitlines() if x] if out.returncode == 0 else []


def in_git() -> bool:
    return bool(git("rev-parse", "--is-inside-work-tree"))


def repo_files() -> list[str]:
    if in_git():
        return git("ls-files", "--cached", "--others", "--exclude-standard")
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            files.append(str(Path(dirpath, f).relative_to(ROOT)))
    return files


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > TEXT_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def scan_files(names: Iterable[str], deny, staged: bool = False) -> list[str]:
    hits: list[str] = []
    for name in names:
        if Path(name).name == ".privacy-denylist":
            continue
        if Path(name).suffix.lower() in ARCHIVE_EXTS:
            hits.append(f"{name}: [archive-file] mail archives must not be committed")
            continue
        if staged:
            out = subprocess.run(["git", "show", f":{name}"], cwd=ROOT, capture_output=True, check=False)
            data = out.stdout
            text = None if b"\x00" in data[:8192] else data.decode("utf-8", errors="replace")
        else:
            text = read_text(ROOT / name)
        if text is not None:
            hits.extend(check_text(name, text, deny))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="files to scan (default: the repository)")
    ap.add_argument("--staged", action="store_true", help="scan the staged versions of files about to be committed")
    ap.add_argument("--stdin", action="store_true", help="scan text from stdin (e.g. `git log -p`)")
    ap.add_argument("--no-denylist", action="store_true", help="generic checks only (what CI runs)")
    args = ap.parse_args(argv)

    if in_git() and ".privacy-denylist" in git("ls-files", ".privacy-denylist"):
        print("REFUSING: .privacy-denylist is tracked by git; remove it from the index (git rm --cached).")
        return 2

    deny = [] if args.no_denylist else load_denylist()
    if args.stdin:
        hits = list(check_text("<stdin>", sys.stdin.read(), deny))
    elif args.staged:
        hits = scan_files(git("diff", "--cached", "--name-only", "--diff-filter=ACMR"), deny, staged=True)
    else:
        hits = scan_files(args.paths or repo_files(), deny)

    for h in hits:
        print(h)
    mode = "generic checks" + (f" + {len(deny)} denylist rules" if deny else (" (no denylist)" if not args.no_denylist else ""))
    print(f"privacy check ({mode}): {'FAILED, ' + str(len(hits)) + ' hit(s)' if hits else 'clean'}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
