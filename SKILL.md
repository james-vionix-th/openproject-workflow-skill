---
name: openproject-workflow
description: Operate OpenProject for delivery coordination using API v3. Use when the user asks to create, update, query, triage, or report on work packages, projects, assignees, statuses, priorities, comments, or schedules in OpenProject, including automation tasks that need deterministic project-state reads and writes.
---

# OpenProject Workflow

## Goal
Execute OpenProject coordination tasks with reproducible API calls and explicit safety checks.

## Guard rails
- Do not hardcode or commit tokens.
- Read before write.
- Scope operations to the intended project unless the user requests cross-project work.
- Preserve concurrency safety on update (`lockVersion`).

## Required configuration
`scripts/openproject_api.py` loads configuration in this order:

1) Process environment (`OPENPROJECT_*`)
2) If a local `openproject.env` file exists, it is loaded and overrides the existing `OPENPROJECT_*` vars.

Where `openproject.env` is searched:
- `./openproject.env` (current working directory)
- `openproject.env` next to `openproject_api.py`

Required keys:
- `OPENPROJECT_BASE_URL`
- `OPENPROJECT_API_KEY`
- `OPENPROJECT_PROJECT_ID`

Optional keys:
- `OPENPROJECT_USER_ID`
- `OPENPROJECT_DEFAULT_TYPE_ID`
- `OPENPROJECT_DEFAULT_PRIORITY_ID`

Authentication model:
- API v3 Basic auth
- Username: `apikey`
- Password: `OPENPROJECT_API_KEY`

## Preferred tooling
Use the bundled script for deterministic API calls:

- `scripts/openproject_api.py`

Examples:

```bash
./scripts/openproject_api.py project-get
./scripts/openproject_api.py wp-list --page-size 100
./scripts/openproject_api.py wp-search-subject --subject-like "lock contention"

./scripts/openproject_api.py wp-create \
  --subject "Investigate production query lock contention" \
  --description-file ./description.md

./scripts/openproject_api.py wp-update \
  --wp-id 123 \
  --subject "Investigate production query lock contention (validated)" \
  --description-file ./update.md \
  --status-id 4

./scripts/openproject_api.py wp-comment \
  --wp-id 123 \
  --body-file ./comment.md
```

Output format for all subcommands:
- JSON object: `{status, data}`

## Supported commands
- `project-get [--project-id]`
- `wp-get --wp-id`
- `wp-list [--project-id] [--page-size]`
- `wp-search-subject [--project-id] --subject-like [--page-size]`
- `wp-create [--project-id] --subject [--type-id] [--description|--description-file|--description-stdin] [--priority-id] [--assignee-id]`
- `wp-update --wp-id [--subject] [--description|--description-file|--description-stdin] [--due-date] [--status-id] [--priority-id] [--assignee-id]`
- `wp-comment --wp-id [--body|--body-file|--body-stdin]`
- `wp-activities --wp-id [--page-size]`

## Shell and quoting safety
- Use `scripts/openproject_api.py` directly; avoid nested wrappers like `bash -lc "zsh -lc ..."`.
- For multiline or quote-heavy text, use `--description-file`, `--body-file`, or `--*-stdin` rather than inline flags.
- Use exactly one source per free-text field (`--description` vs `--description-file` vs `--description-stdin`, same for body).
- Keep command shape stable across runs to reduce escalation/approval churn.

## Operational rules
- Before create, run `wp-search-subject` to check for probable duplicates.
- Before comment, run `wp-activities` and avoid redundant updates.
- For update, the script fetches the current work package and applies the current `lockVersion` automatically.
- On partial update, mutate only requested fields.

## Failure handling
- `401/403`: invalid token or insufficient permissions.
- `404`: wrong ID or wrong project scope.
- `409/422`: lock/version or validation mismatch; re-read entity and retry once.
- `429/5xx`: retry with bounded backoff (`1s`, `2s`, `4s`) then report endpoint and response summary.

## Output contract for the user
For each requested operation, return:
- Action performed (`read`, `create`, `update`, `comment`, `report`)
- Exact target (`project:<id>`, `work_package:<id>`)
- Result summary (status, assignee, priority, due date if available)
- Any unresolved blockers requiring user decision

## Curl fallback (debug only)
Use raw curl only when diagnosing script issues. Prefer JSON payload files, avoid inline escaped JSON, and avoid nested `bash -lc`/`zsh -lc` wrappers.

Canonical shell-safe scaffold:

```bash
set -euo pipefail
set -o noglob
if [ -f "./openproject.env" ]; then
  # shellcheck disable=SC1091
  source "./openproject.env"
fi
OPENPROJECT_AUTH=(--user "apikey:${OPENPROJECT_API_KEY}" -H "Accept: application/hal+json" -H "Content-Type: application/json")
```
