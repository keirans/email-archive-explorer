# Contributing

Thanks for your interest! This project is small and specification-driven; the
design history lives in [`specs/`](./specs/) and the current architecture in
[`docs/architecture.md`](./docs/architecture.md).

## Setup

```bash
uv sync                       # Python 3.11+, installs dev tools (pytest, ruff)
uv run email-archive demo     # fictional demo archive in data/demo/
uv run email-archive serve data/demo/larkspur-mail
```

## Checks

```bash
uv run pytest                                  # unit + API tests against the demo archive
EAE_TEST_PST=/path/to/any.pst uv run pytest    # include PST-specific tests
uv run ruff check
uv run python scripts/privacy_check.py
```

Browser checks (need Google Chrome; not part of `pytest`):

```bash
# Worst-case reading-pane layout (2,000-message thread, 300 recipients, 60 attachments)
uv run --with playwright python scripts/reading_pane_check.py
# Accessibility (axe-core, WCAG 2.1 AA) over every view, both themes; start a server first
uv run --with playwright --with axe-playwright-python python scripts/ui_audit.py http://127.0.0.1:8000/
# Regenerate the README screenshots from the demo archive
uv run --with playwright python scripts/capture_screenshots.py
```

## Keep personal mail out of the repository

This tool exists to read private mail, so the repository has guard rails:

- `data/` and `.cache/` are git-ignored. Never commit an archive, an index, or a
  screenshot of real mail. Use the demo archive for examples, tests and screenshots.
- `scripts/privacy_check.py` flags home-directory paths, non-example email
  addresses and committed archive files. CI runs these generic rules.
- For terms specific to your own mail (names, employers, folder names), create a
  local `.privacy-denylist` (one term per line, or `re:<regex>`). It is
  git-ignored, and the check refuses to run if it is ever tracked. Run the check
  before pushing, or as a pre-commit hook:

  ```bash
  printf '#!/bin/sh\nexec uv run python scripts/privacy_check.py --staged\n' > .git/hooks/pre-commit
  chmod +x .git/hooks/pre-commit
  ```

## Adding an archive format

Implement `BaseArchiveDriver` (see `drivers/base.py` and
[Spec 07](./specs/07-pluggable-archive-driver-architecture.md)), register it in
`drivers/__init__.py`, and add fixtures to `tests/test_drivers.py`. The indexer,
API and UI need no changes.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `test:`, `refactor:` …).
