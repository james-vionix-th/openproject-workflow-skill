#!/usr/bin/env python3
"""Minimal OpenProject API v3 CLI (stdlib-only).

Config rules (mirrors zammad workflow):
- Read config from the current process environment first.
- If a local `openproject.env` file exists, load it and override existing
  `OPENPROJECT_*` vars.
- No references to global system paths; only optional local file + environment.

Required vars:
  OPENPROJECT_BASE_URL
  OPENPROJECT_API_KEY
  OPENPROJECT_PROJECT_ID

Optional vars:
  OPENPROJECT_DEFAULT_TYPE_ID
  OPENPROJECT_DEFAULT_PRIORITY_ID
  OPENPROJECT_USER_ID
"""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

_OPENPROJECT_KEYS = {
    "OPENPROJECT_BASE_URL",
    "OPENPROJECT_API_KEY",
    "OPENPROJECT_PROJECT_ID",
    "OPENPROJECT_DEFAULT_TYPE_ID",
    "OPENPROJECT_DEFAULT_PRIORITY_ID",
    "OPENPROJECT_USER_ID",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; ignores blanks/comments. Supports optional leading 'export '."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return out

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in _OPENPROJECT_KEYS:
            out[k] = v
    return out


def _load_local_env() -> None:
    """If a local openproject.env exists, load and override current environment."""
    candidates = [
        Path.cwd() / "openproject.env",
        Path(__file__).resolve().parent / "openproject.env",
    ]
    for p in candidates:
        if p.exists():
            for k, v in _parse_env_file(p).items():
                os.environ[k] = v
            break


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise SystemExit(f"Missing env var: {name}")
    return value


def _project_id(cli_project_id: int | None) -> int:
    if cli_project_id is not None:
        return cli_project_id
    return int(_env("OPENPROJECT_PROJECT_ID"))


def _read_text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:  # pragma: no cover - CLI error surface
        raise SystemExit(f"Unable to read file '{path}': {e}") from e


def _resolve_text_field(*, inline: str | None, file_path: str | None, from_stdin: bool, field_name: str) -> str:
    sources = int(inline is not None) + int(file_path is not None) + int(from_stdin)
    if sources == 0:
        raise SystemExit(f"Provide one of --{field_name}, --{field_name}-file, or --{field_name}-stdin")
    if sources > 1:
        raise SystemExit(f"Use exactly one of --{field_name}, --{field_name}-file, or --{field_name}-stdin")

    if inline is not None:
        return inline
    if file_path is not None:
        return _read_text_file(file_path)
    return sys.stdin.read()


def _json_or_text(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def request_json(method: str, path: str, *, data=None, params: dict | None = None):
    base = _env("OPENPROJECT_BASE_URL").rstrip("/") + "/"
    token = _env("OPENPROJECT_API_KEY")

    path = path.lstrip("/")
    url = urllib.parse.urljoin(base, path)
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    basic = base64.b64encode(f"apikey:{token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic}",
        "Accept": "application/hal+json",
    }

    body = None
    if method.upper() in {"POST", "PUT", "PATCH"}:
        headers["Content-Type"] = "application/json"
        if data is None:
            data = {}
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, _json_or_text(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if hasattr(e, "read") else ""
        msg = {
            "error": "http_error",
            "status": e.code,
            "reason": getattr(e, "reason", ""),
            "body": _json_or_text(raw),
            "url": url,
            "method": method.upper(),
        }
        return e.code, msg
    except Exception as e:  # pragma: no cover - defensive fallback for CLI
        return 0, {"error": "exception", "message": str(e), "url": url, "method": method.upper()}


def _default_type_id() -> int:
    return int(_env("OPENPROJECT_DEFAULT_TYPE_ID"))


def _href(kind: str, id_value: int | str) -> dict[str, str]:
    return {"href": f"/api/v3/{kind}/{id_value}"}


def _print(status: int, data):
    print(json.dumps({"status": status, "data": data}, indent=2, sort_keys=True))


def _path_from_href(href: str) -> str:
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme and parsed.netloc:
        path = parsed.path or "/"
    else:
        path = href

    if "?" in path:
        return path
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def _href_tail_id(href: str) -> int | None:
    path = urllib.parse.urlparse(href).path if "://" in href else href
    seg = path.rstrip("/").split("/")[-1]
    if seg.isdigit():
        return int(seg)
    return None


def _resource_type_and_id_from_href(href: str) -> tuple[str | None, int | None]:
    path = urllib.parse.urlparse(href).path if "://" in href else href
    segments = [seg for seg in path.split("/") if seg]
    if "api" not in segments:
        return None, None
    try:
        idx = segments.index("v3")
    except ValueError:
        return None, None

    if idx + 1 >= len(segments):
        return None, None

    resource_type = segments[idx + 1]
    resource_id = None
    if idx + 2 < len(segments) and segments[idx + 2].isdigit():
        resource_id = int(segments[idx + 2])

    return resource_type, resource_id


def _notifications_from_hal(data) -> list[dict]:
    if isinstance(data, dict):
        embedded = data.get("_embedded") or {}
        elements = embedded.get("elements")
        if isinstance(elements, list):
            return [x for x in elements if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _collection_elements(data) -> list[dict]:
    if isinstance(data, dict):
        embedded = data.get("_embedded") or {}
        elements = embedded.get("elements")
        if isinstance(elements, list):
            return [x for x in elements if isinstance(x, dict)]
    return []


def _link_ref(links: dict, key: str) -> dict:
    ref = links.get(key)
    if not isinstance(ref, dict):
        return {"id": None, "href": None, "title": None}
    href = ref.get("href") if isinstance(ref.get("href"), str) else None
    title = ref.get("title") if isinstance(ref.get("title"), str) else None
    return {"id": _href_tail_id(href) if href else None, "href": href, "title": title}


def _match_name(title: str | None, query: str, exact: bool) -> bool:
    if not isinstance(title, str):
        return False
    lhs = title.casefold().strip()
    rhs = query.casefold().strip()
    if exact:
        return lhs == rhs
    return rhs in lhs


def _notification_summary(item: dict) -> dict:
    links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
    resource = links.get("resource") if isinstance(links.get("resource"), dict) else {}
    project = links.get("project") if isinstance(links.get("project"), dict) else {}

    resource_href = resource.get("href") if isinstance(resource.get("href"), str) else ""
    project_href = project.get("href") if isinstance(project.get("href"), str) else ""

    resource_type, resource_id = _resource_type_and_id_from_href(resource_href)

    subject = item.get("subject")
    if not isinstance(subject, str) or not subject:
        subject = resource.get("title") if isinstance(resource.get("title"), str) else None

    return {
        "notification_id": item.get("id"),
        "created_at": item.get("createdAt"),
        "reason": item.get("reason"),
        "read_ian": item.get("readIAN"),
        "resource_type": resource_type,
        "resource_id": resource_id,
        "project_id": _href_tail_id(project_href),
        "subject": subject,
    }


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_yyyy_mm_dd(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise SystemExit(f"Invalid date '{raw}', expected YYYY-MM-DD") from e


def _today_utc_date() -> date:
    return datetime.now(timezone.utc).date()


def _wp_link_title(links: dict, key: str) -> str | None:
    ref = links.get(key)
    if not isinstance(ref, dict):
        return None
    title = ref.get("title")
    if isinstance(title, str):
        return title
    return None


def _wp_link_id(links: dict, key: str) -> int | None:
    ref = links.get(key)
    if not isinstance(ref, dict):
        return None
    href = ref.get("href")
    if not isinstance(href, str):
        return None
    return _href_tail_id(href)


def _wp_row(item: dict) -> dict:
    links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
    return {
        "id": item.get("id"),
        "subject": item.get("subject"),
        "status_id": _wp_link_id(links, "status"),
        "status": _wp_link_title(links, "status"),
        "type_id": _wp_link_id(links, "type"),
        "type": _wp_link_title(links, "type"),
        "priority_id": _wp_link_id(links, "priority"),
        "priority": _wp_link_title(links, "priority"),
        "assignee_id": _wp_link_id(links, "assignee"),
        "assignee": _wp_link_title(links, "assignee"),
        "project_id": _wp_link_id(links, "project"),
        "version_id": _wp_link_id(links, "version"),
        "version": _wp_link_title(links, "version"),
        "due_date": item.get("dueDate"),
        "created_at": item.get("createdAt"),
        "updated_at": item.get("updatedAt"),
    }


def _encode_filters(filters: list[dict]) -> str:
    return json.dumps(filters, separators=(",", ":"))


def _project_scoped_filters(project_id: int, filters: list[dict] | None = None) -> list[dict]:
    scoped = [f for f in (filters or []) if isinstance(f, dict)]
    scoped.append({"project": {"operator": "=", "values": [str(project_id)]}})
    return scoped


def _request_project_work_packages_page(
    *,
    project_id: int,
    page_size: int,
    filters: list[dict] | None = None,
) -> tuple[int, object]:
    params = {
        "pageSize": page_size,
        "filters": _encode_filters(_project_scoped_filters(project_id, filters)),
    }
    return request_json("GET", "/api/v3/work_packages", params=params)


def _fetch_work_packages(
    *,
    project_id: int,
    page_size: int,
    max_pages: int,
    filters: list[dict] | None = None,
) -> tuple[int, dict | None, list[dict]]:
    pages = 0
    path = "/api/v3/work_packages"
    params: dict | None = {
        "pageSize": page_size,
        "filters": _encode_filters(_project_scoped_filters(project_id, filters)),
    }
    all_items: list[dict] = []
    last_page = None

    while pages < max_pages:
        status, data = request_json("GET", path, params=params)
        if status < 200 or status >= 300 or not isinstance(data, dict):
            return status, data if isinstance(data, dict) else {"data": data}, []

        last_page = data
        all_items.extend(_collection_elements(data))
        pages += 1

        next_href = (
            (data.get("_links") or {}).get("nextByOffset", {}).get("href")
            if isinstance(data.get("_links"), dict)
            else None
        )
        if not isinstance(next_href, str) or not next_href:
            break

        path = _path_from_href(next_href)
        params = None

    if last_page is None:
        return 0, {"error": "no_pages_fetched"}, []

    return 200, {
        "pages_fetched": pages,
        "source_total": last_page.get("total"),
        "project_id": project_id,
    }, all_items


def _fetch_notifications(*, page_size: int, max_pages: int, reason: str) -> tuple[int, dict | None, list[dict]]:
    pages = 0
    path = "/api/v3/notifications"
    params: dict | None = {"pageSize": page_size}
    all_items: list[dict] = []
    last_page = None

    while pages < max_pages:
        status, data = request_json("GET", path, params=params)
        if status < 200 or status >= 300 or not isinstance(data, dict):
            return status, data if isinstance(data, dict) else {"data": data}, []

        last_page = data
        page_items = _notifications_from_hal(data)
        all_items.extend(page_items)
        pages += 1

        next_href = (
            (data.get("_links") or {}).get("nextByOffset", {}).get("href")
            if isinstance(data.get("_links"), dict)
            else None
        )
        if not isinstance(next_href, str) or not next_href:
            break

        path = _path_from_href(next_href)
        params = None

    if reason == "unread":
        all_items = [item for item in all_items if item.get("readIAN") is False]

    if last_page is None:
        return 0, {"error": "no_pages_fetched"}, []

    return 200, {
        "pages_fetched": pages,
        "requested_reason": reason,
        "source_total": last_page.get("total"),
    }, all_items


def _resolve_single_id_or_error(path: str, *, label: str, name: str, exact: bool, page_size: int) -> tuple[int | None, dict | None]:
    status, data = _resolve_in_collection(path, name=name, exact=exact, page_size=page_size)
    if status != 200 or not isinstance(data, dict):
        return None, {"status": status, "data": data}
    matches = data.get("matches")
    if not isinstance(matches, list):
        return None, {"error": f"{label}_resolve_invalid_result", "query": name}
    if len(matches) == 0:
        return None, {"error": f"{label}_not_found", "query": name}
    if len(matches) > 1:
        return None, {"error": f"{label}_ambiguous", "query": name, "matches": matches}
    match = matches[0]
    return match.get("id"), None


def cmd_project_get(args):
    status, data = request_json("GET", f"/api/v3/projects/{_project_id(args.project_id)}")
    _print(status, data)


def cmd_wp_get(args):
    status, data = request_json("GET", f"/api/v3/work_packages/{args.wp_id}")
    _print(status, data)


def cmd_wp_list(args):
    status, data = _request_project_work_packages_page(
        project_id=_project_id(args.project_id),
        page_size=args.page_size,
    )
    _print(status, data)


def cmd_wp_search_subject(args):
    filters = [{"subject": {"operator": "~", "values": [args.subject_like]}}]
    status, data = _request_project_work_packages_page(
        project_id=_project_id(args.project_id),
        page_size=args.page_size,
        filters=filters,
    )
    _print(status, data)


def cmd_wp_create(args):
    type_id = args.type_id if args.type_id is not None else _default_type_id()
    project_id = _project_id(args.project_id)

    payload = {
        "subject": args.subject,
        "_links": {
            "project": _href("projects", project_id),
            "type": _href("types", type_id),
        },
    }

    if args.description is not None or args.description_file is not None or args.description_stdin:
        description = _resolve_text_field(
            inline=args.description,
            file_path=args.description_file,
            from_stdin=args.description_stdin,
            field_name="description",
        )
        payload["description"] = {"format": "markdown", "raw": description}

    if args.priority_id is not None:
        payload["_links"]["priority"] = _href("priorities", args.priority_id)
    if args.assignee_id is not None:
        payload["_links"]["assignee"] = _href("users", args.assignee_id)

    status, data = request_json("POST", "/api/v3/work_packages", data=payload)
    _print(status, data)


def cmd_wp_update(args):
    status_get, wp = request_json("GET", f"/api/v3/work_packages/{args.wp_id}")
    if status_get < 200 or status_get >= 300 or not isinstance(wp, dict):
        _print(status_get, wp)
        return

    lock_version = wp.get("lockVersion")
    if lock_version is None:
        _print(0, {"error": "missing_lockVersion", "work_package": args.wp_id})
        return

    patch: dict = {"lockVersion": lock_version}

    if args.subject is not None:
        patch["subject"] = args.subject
    if args.description is not None or args.description_file is not None or args.description_stdin:
        description = _resolve_text_field(
            inline=args.description,
            file_path=args.description_file,
            from_stdin=args.description_stdin,
            field_name="description",
        )
        patch["description"] = {"format": "markdown", "raw": description}
    if args.due_date is not None:
        patch["dueDate"] = args.due_date

    links: dict[str, dict[str, str]] = {}
    if args.status_id is not None:
        links["status"] = _href("statuses", args.status_id)
    if args.priority_id is not None:
        links["priority"] = _href("priorities", args.priority_id)
    if args.assignee_id is not None:
        links["assignee"] = _href("users", args.assignee_id)
    if links:
        patch["_links"] = links

    if set(patch.keys()) == {"lockVersion"}:
        raise SystemExit("No fields specified to update")

    status, data = request_json("PATCH", f"/api/v3/work_packages/{args.wp_id}", data=patch)
    _print(status, data)


def cmd_wp_comment(args):
    body = _resolve_text_field(
        inline=args.body,
        file_path=args.body_file,
        from_stdin=args.body_stdin,
        field_name="body",
    )

    payload = {
        "comment": {
            "format": "markdown",
            "raw": body,
        }
    }
    status, data = request_json("POST", f"/api/v3/work_packages/{args.wp_id}/activities", data=payload)
    _print(status, data)


def cmd_wp_activities(args):
    status, data = request_json(
        "GET",
        f"/api/v3/work_packages/{args.wp_id}/activities",
        params={"pageSize": args.page_size},
    )
    _print(status, data)


def cmd_wp_activities_last(args):
    status, data = request_json(
        "GET",
        f"/api/v3/work_packages/{args.wp_id}/activities",
        params={"pageSize": max(args.count, 1)},
    )
    if status < 200 or status >= 300 or not isinstance(data, dict):
        _print(status, data)
        return
    elements = _collection_elements(data)
    rows = []
    for item in elements[: args.count]:
        links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
        user = links.get("user") if isinstance(links.get("user"), dict) else {}
        comment = item.get("comment") if isinstance(item.get("comment"), dict) else {}
        rows.append({
            "id": item.get("id"),
            "created_at": item.get("createdAt"),
            "user": user.get("title"),
            "comment": comment.get("raw"),
            "details_count": len(item.get("details", [])) if isinstance(item.get("details"), list) else 0,
        })
    _print(200, {"work_package_id": args.wp_id, "count": len(rows), "elements": rows})


def cmd_wp_activities_since(args):
    since = _parse_iso_datetime(args.since)
    if since is None:
        raise SystemExit("Invalid --since timestamp, expected ISO8601")
    status, data = request_json(
        "GET",
        f"/api/v3/work_packages/{args.wp_id}/activities",
        params={"pageSize": args.page_size},
    )
    if status < 200 or status >= 300 or not isinstance(data, dict):
        _print(status, data)
        return
    rows = []
    for item in _collection_elements(data):
        created = _parse_iso_datetime(item.get("createdAt"))
        if created is None or created < since:
            continue
        links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
        user = links.get("user") if isinstance(links.get("user"), dict) else {}
        comment = item.get("comment") if isinstance(item.get("comment"), dict) else {}
        rows.append({
            "id": item.get("id"),
            "created_at": item.get("createdAt"),
            "user": user.get("title"),
            "comment": comment.get("raw"),
        })
    _print(200, {"work_package_id": args.wp_id, "since": args.since, "count": len(rows), "elements": rows})


def cmd_wp_find(args):
    project_id = _project_id(args.project_id)
    filters: list[dict] = []
    if args.subject_like:
        filters.append({"subject": {"operator": "~", "values": [args.subject_like]}})
    if args.status_name:
        status_id, err = _resolve_single_id_or_error(
            "/api/v3/statuses",
            label="status",
            name=args.status_name,
            exact=args.exact,
            page_size=args.page_size,
        )
        if err:
            _print(400, err)
            return
        filters.append({"status": {"operator": "=", "values": [str(status_id)]}})
    if args.assignee_id is not None:
        filters.append({"assignee": {"operator": "=", "values": [str(args.assignee_id)]}})
    if args.type_name:
        type_id, err = _resolve_single_id_or_error(
            "/api/v3/types",
            label="type",
            name=args.type_name,
            exact=args.exact,
            page_size=args.page_size,
        )
        if err:
            _print(400, err)
            return
        filters.append({"type": {"operator": "=", "values": [str(type_id)]}})

    status, meta, items = _fetch_work_packages(
        project_id=project_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
        filters=filters or None,
    )
    if status != 200:
        _print(status, meta)
        return
    rows = [_wp_row(x) for x in items]
    _print(200, {"meta": meta, "count": len(rows), "elements": rows})


def _current_user_id() -> int | None:
    if _env("OPENPROJECT_USER_ID", required=False):
        try:
            return int(_env("OPENPROJECT_USER_ID"))
        except ValueError:
            pass
    status, data = request_json("GET", "/api/v3/users/me")
    if status < 200 or status >= 300 or not isinstance(data, dict):
        return None
    uid = data.get("id")
    return uid if isinstance(uid, int) else None


def cmd_wp_list_my_open(args):
    user_id = _current_user_id()
    if user_id is None:
        _print(400, {"error": "cannot_resolve_current_user"})
        return
    project_id = _project_id(args.project_id)
    filters = [{"assignee": {"operator": "=", "values": [str(user_id)]}}]
    status, meta, items = _fetch_work_packages(
        project_id=project_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
        filters=filters,
    )
    if status != 200:
        _print(status, meta)
        return
    rows = [r for r in (_wp_row(x) for x in items) if not (r.get("status") or "").casefold().startswith("closed")]
    _print(200, {"meta": meta, "assignee_id": user_id, "count": len(rows), "elements": rows})


def cmd_wp_due_soon(args):
    project_id = _project_id(args.project_id)
    status, meta, items = _fetch_work_packages(
        project_id=project_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    if status != 200:
        _print(status, meta)
        return
    today = _today_utc_date()
    deadline = today + timedelta(days=args.days)
    rows = []
    for row in (_wp_row(x) for x in items):
        if args.assignee_id is not None and row.get("assignee_id") != args.assignee_id:
            continue
        due_raw = row.get("due_date")
        if not isinstance(due_raw, str):
            continue
        try:
            due = date.fromisoformat(due_raw)
        except ValueError:
            continue
        if today <= due <= deadline:
            rows.append(row)
    _print(200, {"meta": meta, "days": args.days, "count": len(rows), "elements": rows})


def cmd_wp_stale(args):
    project_id = _project_id(args.project_id)
    status, meta, items = _fetch_work_packages(
        project_id=project_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    if status != 200:
        _print(status, meta)
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.inactive_days)
    rows = []
    for row in (_wp_row(x) for x in items):
        updated = _parse_iso_datetime(row.get("updated_at"))
        if updated is None:
            continue
        if updated < cutoff:
            rows.append(row)
    _print(200, {"meta": meta, "inactive_days": args.inactive_days, "count": len(rows), "elements": rows})


def cmd_wp_update_by_name(args):
    status_get, wp = request_json("GET", f"/api/v3/work_packages/{args.wp_id}")
    if status_get < 200 or status_get >= 300 or not isinstance(wp, dict):
        _print(status_get, wp)
        return
    lock_version = wp.get("lockVersion")
    if lock_version is None:
        _print(0, {"error": "missing_lockVersion", "work_package": args.wp_id})
        return
    patch: dict = {"lockVersion": lock_version}
    links: dict[str, dict[str, str]] = {}

    if args.status_name:
        status_id, err = _resolve_single_id_or_error(
            "/api/v3/statuses", label="status", name=args.status_name, exact=args.exact, page_size=args.page_size
        )
        if err:
            _print(400, err)
            return
        links["status"] = _href("statuses", status_id)

    if args.priority_name:
        priority_id, err = _resolve_single_id_or_error(
            "/api/v3/priorities", label="priority", name=args.priority_name, exact=args.exact, page_size=args.page_size
        )
        if err:
            _print(400, err)
            return
        links["priority"] = _href("priorities", priority_id)

    if args.type_name:
        type_id, err = _resolve_single_id_or_error(
            "/api/v3/types", label="type", name=args.type_name, exact=args.exact, page_size=args.page_size
        )
        if err:
            _print(400, err)
            return
        links["type"] = _href("types", type_id)

    if links:
        patch["_links"] = links
    if set(patch.keys()) == {"lockVersion"}:
        raise SystemExit("No named fields specified to update")
    status, data = request_json("PATCH", f"/api/v3/work_packages/{args.wp_id}", data=patch)
    _print(status, data)


def cmd_wp_transition(args):
    status_id, err = _resolve_single_id_or_error(
        "/api/v3/statuses", label="status", name=args.to_status_name, exact=args.exact, page_size=args.page_size
    )
    if err:
        _print(400, err)
        return
    status_get, wp = request_json("GET", f"/api/v3/work_packages/{args.wp_id}")
    if status_get < 200 or status_get >= 300 or not isinstance(wp, dict):
        _print(status_get, wp)
        return
    lock_version = wp.get("lockVersion")
    if lock_version is None:
        _print(0, {"error": "missing_lockVersion", "work_package": args.wp_id})
        return
    patch = {"lockVersion": lock_version, "_links": {"status": _href("statuses", status_id)}}
    status, data = request_json("PATCH", f"/api/v3/work_packages/{args.wp_id}", data=patch)
    _print(status, data)


def _list_reference_collection(path: str, page_size: int) -> tuple[int, object]:
    status, data = request_json("GET", path, params={"pageSize": page_size})
    if status < 200 or status >= 300 or not isinstance(data, dict):
        return status, data

    out = []
    for item in _collection_elements(data):
        out.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "is_default": item.get("isDefault"),
            "is_closed": item.get("isClosed"),
        })
    return 200, {"count": len(out), "elements": out}


def cmd_statuses_list(args):
    status, data = _list_reference_collection("/api/v3/statuses", args.page_size)
    _print(status, data)


def cmd_types_list(args):
    status, data = _list_reference_collection("/api/v3/types", args.page_size)
    _print(status, data)


def cmd_priorities_list(args):
    status, data = _list_reference_collection("/api/v3/priorities", args.page_size)
    _print(status, data)


def _resolve_in_collection(path: str, *, name: str, exact: bool, page_size: int) -> tuple[int, object]:
    status, data = request_json("GET", path, params={"pageSize": page_size})
    if status < 200 or status >= 300 or not isinstance(data, dict):
        return status, data

    matches = []
    for item in _collection_elements(data):
        title = item.get("name") if isinstance(item.get("name"), str) else None
        if _match_name(title, name, exact):
            matches.append({"id": item.get("id"), "name": title})
    return 200, {"query": name, "exact": exact, "count": len(matches), "matches": matches}


def cmd_statuses_resolve(args):
    status, data = _resolve_in_collection("/api/v3/statuses", name=args.name, exact=args.exact, page_size=args.page_size)
    _print(status, data)


def cmd_types_resolve(args):
    status, data = _resolve_in_collection("/api/v3/types", name=args.name, exact=args.exact, page_size=args.page_size)
    _print(status, data)


def cmd_priorities_resolve(args):
    status, data = _resolve_in_collection("/api/v3/priorities", name=args.name, exact=args.exact, page_size=args.page_size)
    _print(status, data)


def cmd_users_list(args):
    status, data = request_json("GET", "/api/v3/users", params={"pageSize": args.page_size})
    if status < 200 or status >= 300 or not isinstance(data, dict):
        _print(status, data)
        return
    out = []
    for item in _collection_elements(data):
        out.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "login": item.get("login"),
            "email": item.get("email"),
        })
    _print(200, {"count": len(out), "elements": out})


def cmd_users_resolve(args):
    status, data = request_json("GET", "/api/v3/users", params={"pageSize": args.page_size})
    if status < 200 or status >= 300 or not isinstance(data, dict):
        _print(status, data)
        return
    q = args.name.casefold().strip()
    matches = []
    for item in _collection_elements(data):
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        login = item.get("login") if isinstance(item.get("login"), str) else ""
        email = item.get("email") if isinstance(item.get("email"), str) else ""
        haystack = [name.casefold(), login.casefold(), email.casefold()]
        if args.exact:
            hit = q in haystack
        else:
            hit = any(q in x for x in haystack)
        if hit:
            matches.append({"id": item.get("id"), "name": name, "login": login, "email": email})
    _print(200, {"query": args.name, "exact": args.exact, "count": len(matches), "matches": matches})


def cmd_versions_list(args):
    project_id = _project_id(args.project_id)
    status, data = request_json("GET", f"/api/v3/projects/{project_id}/versions", params={"pageSize": args.page_size})
    if status < 200 or status >= 300 or not isinstance(data, dict):
        _print(status, data)
        return
    out = []
    for item in _collection_elements(data):
        out.append({"id": item.get("id"), "name": item.get("name"), "status": item.get("status")})
    _print(200, {"project_id": project_id, "count": len(out), "elements": out})


def cmd_versions_resolve(args):
    project_id = _project_id(args.project_id)
    status, data = request_json("GET", f"/api/v3/projects/{project_id}/versions", params={"pageSize": args.page_size})
    if status < 200 or status >= 300 or not isinstance(data, dict):
        _print(status, data)
        return
    matches = []
    for item in _collection_elements(data):
        title = item.get("name") if isinstance(item.get("name"), str) else None
        if _match_name(title, args.name, args.exact):
            matches.append({"id": item.get("id"), "name": title, "status": item.get("status")})
    _print(200, {"project_id": project_id, "query": args.name, "exact": args.exact, "count": len(matches), "matches": matches})


def cmd_wp_context(args):
    status, raw = request_json("GET", f"/api/v3/work_packages/{args.wp_id}")
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return

    links = raw.get("_links") if isinstance(raw.get("_links"), dict) else {}
    data = {
        "work_package_id": raw.get("id"),
        "subject": raw.get("subject"),
        "lock_version": raw.get("lockVersion"),
        "project": _link_ref(links, "project"),
        "status": _link_ref(links, "status"),
        "type": _link_ref(links, "type"),
        "priority": _link_ref(links, "priority"),
        "assignee": _link_ref(links, "assignee"),
        "version": _link_ref(links, "version"),
        "author": _link_ref(links, "author"),
    }
    _print(200, data)


def cmd_notifications_list(args):
    if args.all_pages:
        status, meta, items = _fetch_notifications(
            page_size=args.page_size,
            max_pages=args.max_pages,
            reason=args.reason,
        )
        if status != 200:
            _print(status, meta)
            return
        data = {
            "meta": meta,
            "elements": [_notification_summary(item) for item in items],
            "count": len(items),
        }
        _print(200, data)
        return

    status, raw = request_json("GET", "/api/v3/notifications", params={"pageSize": args.page_size})
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return

    items = _notifications_from_hal(raw)
    if args.reason == "unread":
        items = [item for item in items if item.get("readIAN") is False]

    data = {
        "meta": {
            "pages_fetched": 1,
            "requested_reason": args.reason,
            "source_total": raw.get("total"),
        },
        "elements": [_notification_summary(item) for item in items],
        "count": len(items),
    }
    _print(200, data)


def cmd_notifications_get(args):
    status, raw = request_json("GET", f"/api/v3/notifications/{args.notification_id}")
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return
    _print(200, _notification_summary(raw))


def cmd_notifications_unread_count(args):
    status, meta, items = _fetch_notifications(
        page_size=args.page_size,
        max_pages=args.max_pages,
        reason="all",
    )
    if status != 200:
        _print(status, meta)
        return

    unread = sum(1 for item in items if item.get("readIAN") is False)
    _print(200, {"unread_count": unread, "meta": meta})


def _set_notification_read_state(notification_id: int, read_ian: bool) -> tuple[int, object]:
    action = "read_ian" if read_ian else "unread_ian"
    return request_json("POST", f"/api/v3/notifications/{notification_id}/{action}")


def _set_notification_collection_read_state(read_ian: bool, *, filters: list[dict] | None = None) -> tuple[int, object]:
    action = "read_ian" if read_ian else "unread_ian"
    params = None
    if filters:
        params = {"filters": json.dumps(filters, separators=(",", ":"))}
    return request_json("POST", f"/api/v3/notifications/{action}", params=params)


def cmd_notifications_mark_read(args):
    status, data = _set_notification_read_state(args.notification_id, True)
    _print(status, data)


def cmd_notifications_mark_unread(args):
    status, data = _set_notification_read_state(args.notification_id, False)
    _print(status, data)


def cmd_notifications_mark_all_read(args):
    status, meta, items = _fetch_notifications(
        page_size=args.page_size,
        max_pages=args.max_pages,
        reason="unread",
    )
    if status != 200:
        _print(status, meta)
        return

    targets = [item for item in items if isinstance(item.get("id"), int)]
    if args.dry_run:
        _print(200, {
            "dry_run": True,
            "target_notification_ids": [item["id"] for item in targets],
            "target_count": len(targets),
            "meta": meta,
        })
        return

    if not targets:
        _print(200, {
            "updated_notification_ids": [],
            "updated_count": 0,
            "meta": meta,
        })
        return

    # Limit bulk operation to the unresolved notification ids discovered in this run.
    filters = [{"id": {"operator": "=", "values": [str(item["id"]) for item in targets]}}]
    st, payload = _set_notification_collection_read_state(True, filters=filters)
    if 200 <= st < 300:
        _print(st, {
            "updated_notification_ids": [item["id"] for item in targets],
            "updated_count": len(targets),
            "meta": meta,
        })
        return

    _print(st, {
        "error": "bulk_mark_read_failed",
        "target_notification_ids": [item["id"] for item in targets],
        "target_count": len(targets),
        "meta": meta,
        "data": payload,
    })


def cmd_notifications_resolve_target(args):
    status, raw = request_json("GET", f"/api/v3/notifications/{args.notification_id}")
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return

    summary = _notification_summary(raw)
    links = raw.get("_links") if isinstance(raw.get("_links"), dict) else {}
    resource = links.get("resource") if isinstance(links.get("resource"), dict) else {}
    resource_href = resource.get("href") if isinstance(resource.get("href"), str) else None

    target = {
        "notification_id": summary["notification_id"],
        "resource_href": resource_href,
        "resource_type": summary["resource_type"],
        "resource_id": summary["resource_id"],
        "project_id": summary["project_id"],
        "subject": summary["subject"],
    }
    _print(200, target)


def cmd_notifications_last(args):
    status, raw = request_json("GET", "/api/v3/notifications", params={"pageSize": max(args.count, 1)})
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return
    items = _notifications_from_hal(raw)
    if args.reason == "unread":
        items = [item for item in items if item.get("readIAN") is False]
    out = [_notification_summary(item) for item in items[: args.count]]
    _print(200, {"count": len(out), "elements": out})


def cmd_notifications_triage(args):
    status, raw = request_json("GET", "/api/v3/notifications", params={"pageSize": max(args.count, 1)})
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return
    items = _notifications_from_hal(raw)
    if args.reason == "unread":
        items = [item for item in items if item.get("readIAN") is False]
    rows = []
    for item in items[: args.count]:
        summary = _notification_summary(item)
        tri = {"notification": summary}
        if summary.get("resource_type") == "work_packages" and isinstance(summary.get("resource_id"), int):
            st, wp = request_json("GET", f"/api/v3/work_packages/{summary['resource_id']}")
            if 200 <= st < 300 and isinstance(wp, dict):
                tri["work_package"] = _wp_row(wp)
            else:
                tri["work_package_error"] = {"status": st, "data": wp}
        rows.append(tri)
    _print(200, {"count": len(rows), "elements": rows})


def cmd_notifications_mark_resolved(args):
    status, raw = request_json("GET", f"/api/v3/notifications/{args.notification_id}")
    if status < 200 or status >= 300 or not isinstance(raw, dict):
        _print(status, raw)
        return
    summary = _notification_summary(raw)
    if summary.get("resource_type") != "work_packages" or not isinstance(summary.get("resource_id"), int):
        _print(409, {"error": "notification_resource_not_work_package", "notification": summary})
        return
    wp_id = summary["resource_id"]
    st_wp, wp = request_json("GET", f"/api/v3/work_packages/{wp_id}")
    if st_wp < 200 or st_wp >= 300 or not isinstance(wp, dict):
        _print(st_wp, wp)
        return
    row = _wp_row(wp)
    if (row.get("status") or "").casefold().strip() != args.if_wp_status.casefold().strip():
        _print(409, {"error": "status_condition_not_met", "required_status": args.if_wp_status, "actual_status": row.get("status"), "work_package_id": wp_id})
        return
    st_mark, mark = _set_notification_read_state(args.notification_id, True)
    _print(st_mark, {"notification_id": args.notification_id, "work_package_id": wp_id, "mark_result": mark})


def _filter_rows_since(rows: list[dict], since_date: date) -> list[dict]:
    out = []
    for row in rows:
        created = _parse_iso_datetime(row.get("created_at"))
        updated = _parse_iso_datetime(row.get("updated_at"))
        if (created and created.date() >= since_date) or (updated and updated.date() >= since_date):
            out.append(row)
    return out


def cmd_report_daily(args):
    project_id = _project_id(args.project_id)
    since = _parse_yyyy_mm_dd(args.since) if args.since else (_today_utc_date() - timedelta(days=1))
    status, meta, items = _fetch_work_packages(
        project_id=project_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    if status != 200:
        _print(status, meta)
        return
    rows = [_wp_row(x) for x in items]
    scoped = _filter_rows_since(rows, since)
    created = 0
    updated = 0
    closed = 0
    high_open = []
    for row in scoped:
        created_dt = _parse_iso_datetime(row.get("created_at"))
        updated_dt = _parse_iso_datetime(row.get("updated_at"))
        if created_dt and created_dt.date() >= since:
            created += 1
        if updated_dt and updated_dt.date() >= since:
            updated += 1
        status_name = (row.get("status") or "").casefold()
        if status_name.startswith("closed"):
            closed += 1
        priority_name = (row.get("priority") or "").casefold()
        if "high" in priority_name and not status_name.startswith("closed"):
            high_open.append(row)
    _print(200, {
        "project_id": project_id,
        "since": since.isoformat(),
        "created_count": created,
        "updated_count": updated,
        "closed_count": closed,
        "high_open_count": len(high_open),
        "high_open": high_open[: args.limit],
        "meta": meta,
    })


def cmd_report_assignee(args):
    project_id = _project_id(args.project_id)
    since = _parse_yyyy_mm_dd(args.since)
    status, meta, items = _fetch_work_packages(
        project_id=project_id,
        page_size=args.page_size,
        max_pages=args.max_pages,
    )
    if status != 200:
        _print(status, meta)
        return
    rows = []
    for item in items:
        row = _wp_row(item)
        if row.get("assignee_id") == args.assignee_id:
            rows.append(row)
    scoped = _filter_rows_since(rows, since)
    open_rows = [r for r in rows if not (r.get("status") or "").casefold().startswith("closed")]
    updated_recent = [r for r in scoped if _parse_iso_datetime(r.get("updated_at")) is not None]
    _print(200, {
        "project_id": project_id,
        "assignee_id": args.assignee_id,
        "since": since.isoformat(),
        "backlog_open_count": len(open_rows),
        "updated_since_count": len(updated_recent),
        "recent_updates": updated_recent[: args.limit],
        "meta": meta,
    })


def build_parser():
    p = argparse.ArgumentParser(prog="openproject_api.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("project-get", help="Get project details")
    sp.add_argument("--project-id", type=int)
    sp.set_defaults(fn=cmd_project_get)

    sp = sub.add_parser("wp-get", help="Get one work package")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.set_defaults(fn=cmd_wp_get)

    sp = sub.add_parser("wp-list", help="List project work packages")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--page-size", type=int, default=100)
    sp.set_defaults(fn=cmd_wp_list)

    sp = sub.add_parser("wp-search-subject", help="Search by partial subject match")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--subject-like", required=True)
    sp.add_argument("--page-size", type=int, default=100)
    sp.set_defaults(fn=cmd_wp_search_subject)

    sp = sub.add_parser("wp-create", help="Create a work package")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--subject", required=True)
    sp.add_argument("--type-id", type=int)
    sp.add_argument("--description")
    sp.add_argument("--description-file", help="Read description text from file")
    sp.add_argument("--description-stdin", action="store_true", help="Read description text from stdin")
    sp.add_argument("--priority-id", type=int)
    sp.add_argument("--assignee-id", type=int)
    sp.set_defaults(fn=cmd_wp_create)

    sp = sub.add_parser("wp-update", help="Patch a work package")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--subject")
    sp.add_argument("--description")
    sp.add_argument("--description-file", help="Read description text from file")
    sp.add_argument("--description-stdin", action="store_true", help="Read description text from stdin")
    sp.add_argument("--due-date", help="YYYY-MM-DD")
    sp.add_argument("--status-id", type=int)
    sp.add_argument("--priority-id", type=int)
    sp.add_argument("--assignee-id", type=int)
    sp.set_defaults(fn=cmd_wp_update)

    sp = sub.add_parser("wp-comment", help="Post a work package activity comment")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--body")
    sp.add_argument("--body-file", help="Read comment body from file")
    sp.add_argument("--body-stdin", action="store_true", help="Read comment body from stdin")
    sp.set_defaults(fn=cmd_wp_comment)

    sp = sub.add_parser("wp-activities", help="List work package activities")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--page-size", type=int, default=20)
    sp.set_defaults(fn=cmd_wp_activities)

    sp = sub.add_parser("wp-activities-last", help="Return the latest N activities for a work package")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--count", type=int, default=5)
    sp.set_defaults(fn=cmd_wp_activities_last)

    sp = sub.add_parser("wp-activities-since", help="Return work package activities since ISO8601 timestamp")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--since", required=True, help="ISO8601 timestamp")
    sp.add_argument("--page-size", type=int, default=100)
    sp.set_defaults(fn=cmd_wp_activities_since)

    sp = sub.add_parser("wp-find", help="Find work packages using combined filters")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--subject-like")
    sp.add_argument("--status-name")
    sp.add_argument("--assignee-id", type=int)
    sp.add_argument("--type-name")
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--page-size", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=10)
    sp.set_defaults(fn=cmd_wp_find)

    sp = sub.add_parser("wp-list-my-open", help="List open work packages assigned to current user")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--page-size", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=10)
    sp.set_defaults(fn=cmd_wp_list_my_open)

    sp = sub.add_parser("wp-due-soon", help="List work packages due within N days")
    sp.add_argument("--days", type=int, required=True)
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--assignee-id", type=int)
    sp.add_argument("--page-size", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=10)
    sp.set_defaults(fn=cmd_wp_due_soon)

    sp = sub.add_parser("wp-stale", help="List work packages with no updates for N days")
    sp.add_argument("--inactive-days", type=int, required=True)
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--page-size", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=10)
    sp.set_defaults(fn=cmd_wp_stale)

    sp = sub.add_parser("wp-transition", help="Transition a work package to a named status")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--to-status-name", required=True)
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_wp_transition)

    sp = sub.add_parser("wp-update-by-name", help="Update status/type/priority by display names")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.add_argument("--status-name")
    sp.add_argument("--priority-name")
    sp.add_argument("--type-name")
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_wp_update_by_name)

    sp = sub.add_parser("wp-context", help="Resolve linked status/type/priority and related refs for one work package")
    sp.add_argument("--wp-id", type=int, required=True)
    sp.set_defaults(fn=cmd_wp_context)

    sp = sub.add_parser("statuses-list", help="List available work package statuses")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_statuses_list)

    sp = sub.add_parser("statuses-resolve", help="Resolve status IDs by status name")
    sp.add_argument("--name", required=True)
    sp.add_argument("--exact", action="store_true", help="Require exact case-insensitive name match")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_statuses_resolve)

    sp = sub.add_parser("types-list", help="List available work package types")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_types_list)

    sp = sub.add_parser("types-resolve", help="Resolve type IDs by type name")
    sp.add_argument("--name", required=True)
    sp.add_argument("--exact", action="store_true", help="Require exact case-insensitive name match")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_types_resolve)

    sp = sub.add_parser("priorities-list", help="List available priorities")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_priorities_list)

    sp = sub.add_parser("priorities-resolve", help="Resolve priority IDs by priority name")
    sp.add_argument("--name", required=True)
    sp.add_argument("--exact", action="store_true", help="Require exact case-insensitive name match")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_priorities_resolve)

    sp = sub.add_parser("users-list", help="List users")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_users_list)

    sp = sub.add_parser("users-resolve", help="Resolve users by name/login/email")
    sp.add_argument("--name", required=True)
    sp.add_argument("--exact", action="store_true", help="Require exact case-insensitive match")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_users_resolve)

    sp = sub.add_parser("versions-list", help="List versions for a project")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_versions_list)

    sp = sub.add_parser("versions-resolve", help="Resolve version IDs by name")
    sp.add_argument("--name", required=True)
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--exact", action="store_true", help="Require exact case-insensitive name match")
    sp.add_argument("--page-size", type=int, default=200)
    sp.set_defaults(fn=cmd_versions_resolve)

    sp = sub.add_parser("notifications-list", help="List notifications with deterministic fields")
    sp.add_argument("--page-size", type=int, default=50)
    sp.add_argument("--reason", choices=["unread", "all"], default="unread")
    sp.add_argument("--all-pages", action="store_true", help="Follow nextByOffset links up to --max-pages")
    sp.add_argument("--max-pages", type=int, default=20)
    sp.set_defaults(fn=cmd_notifications_list)

    sp = sub.add_parser("notifications-get", help="Get a single notification by id")
    sp.add_argument("--notification-id", type=int, required=True)
    sp.set_defaults(fn=cmd_notifications_get)

    sp = sub.add_parser("notifications-unread-count", help="Count unread notifications")
    sp.add_argument("--page-size", type=int, default=50)
    sp.add_argument("--max-pages", type=int, default=20)
    sp.set_defaults(fn=cmd_notifications_unread_count)

    sp = sub.add_parser("notifications-mark-read", help="Mark one notification as read")
    sp.add_argument("--notification-id", type=int, required=True)
    sp.set_defaults(fn=cmd_notifications_mark_read)

    sp = sub.add_parser("notifications-mark-unread", help="Mark one notification as unread")
    sp.add_argument("--notification-id", type=int, required=True)
    sp.set_defaults(fn=cmd_notifications_mark_unread)

    sp = sub.add_parser("notifications-mark-all-read", help="Mark all unread notifications as read")
    sp.add_argument("--page-size", type=int, default=50)
    sp.add_argument("--max-pages", type=int, default=20)
    sp.add_argument("--dry-run", action="store_true", help="List target ids without mutating")
    sp.set_defaults(fn=cmd_notifications_mark_all_read)

    sp = sub.add_parser("notifications-resolve-target", help="Resolve notification target resource")
    sp.add_argument("--notification-id", type=int, required=True)
    sp.set_defaults(fn=cmd_notifications_resolve_target)

    sp = sub.add_parser("notifications-last", help="Return latest notifications")
    sp.add_argument("--count", type=int, default=10)
    sp.add_argument("--reason", choices=["unread", "all"], default="all")
    sp.set_defaults(fn=cmd_notifications_last)

    sp = sub.add_parser("notifications-triage", help="Return notifications with resolved work package context")
    sp.add_argument("--count", type=int, default=10)
    sp.add_argument("--reason", choices=["unread", "all"], default="unread")
    sp.set_defaults(fn=cmd_notifications_triage)

    sp = sub.add_parser("notifications-mark-resolved", help="Mark notification read only when linked work package status matches")
    sp.add_argument("--notification-id", type=int, required=True)
    sp.add_argument("--if-wp-status", required=True)
    sp.set_defaults(fn=cmd_notifications_mark_resolved)

    sp = sub.add_parser("report-daily", help="Daily summary for project changes")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--since", help="YYYY-MM-DD; defaults to yesterday UTC")
    sp.add_argument("--page-size", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=10)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_report_daily)

    sp = sub.add_parser("report-assignee", help="Assignee-focused backlog and update summary")
    sp.add_argument("--assignee-id", type=int, required=True)
    sp.add_argument("--since", required=True, help="YYYY-MM-DD")
    sp.add_argument("--project-id", type=int)
    sp.add_argument("--page-size", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=10)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_report_assignee)

    return p


def main(argv):
    _load_local_env()

    parser = build_parser()
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
