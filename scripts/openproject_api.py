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


def cmd_project_get(args):
    status, data = request_json("GET", f"/api/v3/projects/{_project_id(args.project_id)}")
    _print(status, data)


def cmd_wp_get(args):
    status, data = request_json("GET", f"/api/v3/work_packages/{args.wp_id}")
    _print(status, data)


def cmd_wp_list(args):
    params = {"pageSize": args.page_size}
    status, data = request_json(
        "GET",
        f"/api/v3/projects/{_project_id(args.project_id)}/work_packages",
        params=params,
    )
    _print(status, data)


def cmd_wp_search_subject(args):
    filters = [{"subject": {"operator": "~", "values": [args.subject_like]}}]
    params = {
        "pageSize": args.page_size,
        "filters": json.dumps(filters, separators=(",", ":")),
    }
    status, data = request_json(
        "GET",
        f"/api/v3/projects/{_project_id(args.project_id)}/work_packages",
        params=params,
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

    return p


def main(argv):
    _load_local_env()

    parser = build_parser()
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
