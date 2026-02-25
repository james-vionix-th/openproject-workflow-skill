import importlib.util
import json
import os
import pathlib
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest import mock


def _load_module():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts" / "openproject_api.py"
    spec = importlib.util.spec_from_file_location("openproject_api", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OpenProjectApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_parse_iso_datetime_zulu(self):
        dt = self.mod._parse_iso_datetime("2026-02-20T12:34:56Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo.utcoffset(dt).total_seconds(), 0)

    def test_parse_iso_datetime_invalid(self):
        self.assertIsNone(self.mod._parse_iso_datetime("not-a-date"))

    def test_href_tail_id(self):
        self.assertEqual(self.mod._href_tail_id("/api/v3/work_packages/60"), 60)
        self.assertEqual(self.mod._href_tail_id("https://x/api/v3/statuses/15"), 15)
        self.assertIsNone(self.mod._href_tail_id("/api/v3/statuses/open"))

    def test_resource_type_and_id_from_href(self):
        rtype, rid = self.mod._resource_type_and_id_from_href("/api/v3/work_packages/60")
        self.assertEqual(rtype, "work_packages")
        self.assertEqual(rid, 60)

    def test_match_name(self):
        self.assertTrue(self.mod._match_name("In Progress", "progress", exact=False))
        self.assertFalse(self.mod._match_name("In Progress", "progress", exact=True))
        self.assertTrue(self.mod._match_name("In Progress", "in progress", exact=True))

    def test_notification_summary(self):
        payload = {
            "id": 99,
            "createdAt": "2026-02-20T01:02:03Z",
            "reason": "mentioned",
            "readIAN": False,
            "_links": {
                "resource": {"href": "/api/v3/work_packages/60", "title": "WP title"},
                "project": {"href": "/api/v3/projects/7"},
            },
        }
        out = self.mod._notification_summary(payload)
        self.assertEqual(out["notification_id"], 99)
        self.assertEqual(out["resource_type"], "work_packages")
        self.assertEqual(out["resource_id"], 60)
        self.assertEqual(out["project_id"], 7)
        self.assertEqual(out["subject"], "WP title")

    def test_set_notification_read_state_uses_action_endpoint(self):
        with mock.patch.object(self.mod, "request_json", return_value=(200, {"ok": True})) as req:
            status, data = self.mod._set_notification_read_state(123, True)
        self.assertEqual(status, 200)
        self.assertEqual(data["ok"], True)
        req.assert_called_once_with("POST", "/api/v3/notifications/123/read_ian")

    def test_set_notification_collection_read_state_with_filters(self):
        filters = [{"id": {"operator": "=", "values": ["1"]}}]
        with mock.patch.object(self.mod, "request_json", return_value=(200, {"ok": True})) as req:
            self.mod._set_notification_collection_read_state(True, filters=filters)
        req.assert_called_once()
        args, kwargs = req.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/api/v3/notifications/read_ian")
        self.assertIn("params", kwargs)
        self.assertIn("filters", kwargs["params"])

    def test_wp_context_resolves_links(self):
        args = SimpleNamespace(wp_id=60)
        payload = {
            "id": 60,
            "subject": "Test WP",
            "lockVersion": 3,
            "_links": {
                "project": {"href": "/api/v3/projects/7", "title": "P"},
                "status": {"href": "/api/v3/statuses/15", "title": "Review"},
                "type": {"href": "/api/v3/types/1", "title": "Task"},
            },
        }
        with mock.patch.object(self.mod, "request_json", return_value=(200, payload)), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_wp_context(args)
        p.assert_called_once()
        printed = p.call_args.args[1]
        self.assertEqual(printed["status"]["id"], 15)
        self.assertEqual(printed["type"]["title"], "Task")

    def test_wp_list_my_open_filters_closed(self):
        args = SimpleNamespace(project_id=7, page_size=50, max_pages=2)
        items = [
            {"id": 1, "subject": "Open", "_links": {"status": {"href": "/api/v3/statuses/2", "title": "In progress"}}},
            {"id": 2, "subject": "Closed", "_links": {"status": {"href": "/api/v3/statuses/3", "title": "Closed"}}},
        ]
        with mock.patch.object(self.mod, "_current_user_id", return_value=42), mock.patch.object(
            self.mod, "_fetch_work_packages", return_value=(200, {"ok": True}, items)
        ), mock.patch.object(self.mod, "_print") as p:
            self.mod.cmd_wp_list_my_open(args)
        printed = p.call_args.args[1]
        self.assertEqual(printed["assignee_id"], 42)
        self.assertEqual(printed["count"], 1)
        self.assertEqual(printed["elements"][0]["id"], 1)

    def test_wp_list_uses_workspace_collection(self):
        args = SimpleNamespace(project_id=7, page_size=20)
        global_ok = {"_embedded": {"elements": []}, "total": 0}

        with mock.patch.object(self.mod, "request_json", return_value=(200, global_ok)) as req, mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_wp_list(args)

        req.assert_called_once()
        self.assertEqual(req.call_args.args[1], "/api/v3/workspaces/7/work_packages")
        self.assertEqual(req.call_args.kwargs["params"], {"pageSize": 20})
        self.assertEqual(p.call_args.args[0], 200)

    def test_wp_search_subject_uses_workspace_collection_with_subject_filter(self):
        args = SimpleNamespace(project_id=7, page_size=20, subject_like="pcn")
        global_ok = {"_embedded": {"elements": []}, "total": 0}

        with mock.patch.object(self.mod, "request_json", return_value=(200, global_ok)) as req, mock.patch.object(
            self.mod, "_print"
        ):
            self.mod.cmd_wp_search_subject(args)

        req.assert_called_once()
        self.assertEqual(req.call_args.args[1], "/api/v3/workspaces/7/work_packages")
        filters = json.loads(req.call_args.kwargs["params"]["filters"])
        self.assertEqual(filters, [{"subject": {"operator": "~", "values": ["pcn"]}}])

    def test_fetch_work_packages_uses_workspace_collection(self):
        page = {"_embedded": {"elements": [{"id": 1}]}, "_links": {}, "total": 1}
        with mock.patch.object(self.mod, "request_json", return_value=(200, page)) as req:
            status, meta, items = self.mod._fetch_work_packages(project_id=7, page_size=5, max_pages=2, filters=None)

        self.assertEqual(status, 200)
        self.assertEqual(meta["project_id"], 7)
        self.assertEqual([x["id"] for x in items], [1])
        self.assertEqual(req.call_args.args[1], "/api/v3/workspaces/7/work_packages")
        self.assertEqual(req.call_args.kwargs["params"], {"pageSize": 5})

    def test_versions_list_uses_workspace_endpoint(self):
        args = SimpleNamespace(project_id=7, page_size=20)
        payload = {"_embedded": {"elements": [{"id": 1, "name": "v1", "status": "open"}]}}
        with mock.patch.object(self.mod, "request_json", return_value=(200, payload)) as req, mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_versions_list(args)
        req.assert_called_once_with("GET", "/api/v3/workspaces/7/versions", params={"pageSize": 20})
        self.assertEqual(p.call_args.args[0], 200)
        self.assertEqual(p.call_args.args[1]["count"], 1)

    def test_versions_resolve_uses_workspace_endpoint(self):
        args = SimpleNamespace(project_id=7, page_size=20, name="v", exact=False)
        payload = {
            "_embedded": {
                "elements": [
                    {"id": 1, "name": "v1", "status": "open"},
                    {"id": 2, "name": "release", "status": "closed"},
                ]
            }
        }
        with mock.patch.object(self.mod, "request_json", return_value=(200, payload)) as req, mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_versions_resolve(args)
        req.assert_called_once_with("GET", "/api/v3/workspaces/7/versions", params={"pageSize": 20})
        self.assertEqual(p.call_args.args[0], 200)
        self.assertEqual(p.call_args.args[1]["count"], 1)

    def test_notifications_triage_enriches_work_package(self):
        args = SimpleNamespace(count=3, reason="all")
        notif_page = {
            "_embedded": {
                "elements": [
                    {
                        "id": 11,
                        "createdAt": "2026-02-20T00:00:00Z",
                        "reason": "mentioned",
                        "readIAN": False,
                        "_links": {
                            "resource": {"href": "/api/v3/work_packages/60", "title": "WP 60"},
                            "project": {"href": "/api/v3/projects/7", "title": "P"},
                        },
                    }
                ]
            }
        }
        wp_payload = {
            "id": 60,
            "subject": "WP 60",
            "_links": {
                "status": {"href": "/api/v3/statuses/2", "title": "In progress"},
                "type": {"href": "/api/v3/types/1", "title": "Task"},
                "priority": {"href": "/api/v3/priorities/3", "title": "Normal"},
            },
        }

        def fake_request(method, path, **kwargs):
            if path == "/api/v3/notifications":
                return 200, notif_page
            if path == "/api/v3/work_packages/60":
                return 200, wp_payload
            return 404, {"error": "not found"}

        with mock.patch.object(self.mod, "request_json", side_effect=fake_request), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_notifications_triage(args)
        printed = p.call_args.args[1]
        self.assertEqual(printed["count"], 1)
        self.assertEqual(printed["elements"][0]["work_package"]["id"], 60)

    def test_wp_activities_since_filters_by_timestamp(self):
        args = SimpleNamespace(wp_id=60, since="2026-02-20T00:00:00Z", page_size=50)
        payload = {
            "_embedded": {
                "elements": [
                    {"id": 1, "createdAt": "2026-02-19T23:59:59Z", "comment": {"raw": "old"}},
                    {
                        "id": 2,
                        "createdAt": "2026-02-20T00:00:00Z",
                        "comment": {"raw": "new"},
                        "_links": {"user": {"title": "Alice"}},
                    },
                ]
            }
        }
        with mock.patch.object(self.mod, "request_json", return_value=(200, payload)), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_wp_activities_since(args)
        printed = p.call_args.args[1]
        self.assertEqual(printed["count"], 1)
        self.assertEqual(printed["elements"][0]["id"], 2)

    def test_wp_find_returns_400_when_status_name_ambiguous(self):
        args = SimpleNamespace(
            project_id=7,
            subject_like=None,
            status_name="review",
            assignee_id=None,
            type_name=None,
            exact=False,
            page_size=50,
            max_pages=2,
        )
        with mock.patch.object(
            self.mod,
            "_resolve_single_id_or_error",
            return_value=(None, {"error": "status_ambiguous", "matches": [{"id": 1}, {"id": 2}]}),
        ), mock.patch.object(self.mod, "_print") as p:
            self.mod.cmd_wp_find(args)
        p.assert_called_once()
        self.assertEqual(p.call_args.args[0], 400)

    def test_wp_due_soon_filters_by_range_and_assignee(self):
        args = SimpleNamespace(project_id=7, days=7, assignee_id=42, page_size=50, max_pages=2)
        items = [
            {
                "id": 1,
                "subject": "in-range",
                "dueDate": "2026-02-22",
                "_links": {"assignee": {"href": "/api/v3/users/42", "title": "A"}},
            },
            {
                "id": 2,
                "subject": "wrong-assignee",
                "dueDate": "2026-02-22",
                "_links": {"assignee": {"href": "/api/v3/users/99", "title": "B"}},
            },
            {"id": 3, "subject": "out-of-range", "dueDate": "2026-03-10", "_links": {"assignee": {"href": "/api/v3/users/42"}}},
        ]
        with mock.patch.object(self.mod, "_today_utc_date", return_value=self.mod.date(2026, 2, 20)), mock.patch.object(
            self.mod, "_fetch_work_packages", return_value=(200, {"ok": True}, items)
        ), mock.patch.object(self.mod, "_print") as p:
            self.mod.cmd_wp_due_soon(args)
        printed = p.call_args.args[1]
        self.assertEqual(printed["count"], 1)
        self.assertEqual(printed["elements"][0]["id"], 1)

    def test_notifications_mark_all_read_dry_run(self):
        args = SimpleNamespace(page_size=50, max_pages=5, dry_run=True)
        items = [{"id": 11, "readIAN": False}, {"id": 12, "readIAN": False}]
        with mock.patch.object(
            self.mod, "_fetch_notifications", return_value=(200, {"source_total": 2}, items)
        ), mock.patch.object(self.mod, "_print") as p:
            self.mod.cmd_notifications_mark_all_read(args)
        self.assertEqual(p.call_args.args[0], 200)
        printed = p.call_args.args[1]
        self.assertTrue(printed["dry_run"])
        self.assertEqual(printed["target_count"], 2)

    def test_notifications_mark_all_read_success(self):
        args = SimpleNamespace(page_size=50, max_pages=5, dry_run=False)
        items = [{"id": 11, "readIAN": False}, {"id": 12, "readIAN": False}]
        with mock.patch.object(
            self.mod, "_fetch_notifications", return_value=(200, {"source_total": 2}, items)
        ), mock.patch.object(self.mod, "_set_notification_collection_read_state", return_value=(200, {"ok": True})), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_notifications_mark_all_read(args)
        self.assertEqual(p.call_args.args[0], 200)
        printed = p.call_args.args[1]
        self.assertEqual(printed["updated_count"], 2)

    def test_notifications_mark_resolved_conflict_on_status_mismatch(self):
        args = SimpleNamespace(notification_id=55, if_wp_status="Closed")
        notification = {
            "id": 55,
            "_links": {
                "resource": {"href": "/api/v3/work_packages/60", "title": "WP"},
                "project": {"href": "/api/v3/projects/7"},
            },
        }
        wp = {
            "id": 60,
            "_links": {"status": {"href": "/api/v3/statuses/2", "title": "In progress"}},
        }

        def fake_request(method, path, **kwargs):
            if path == "/api/v3/notifications/55":
                return 200, notification
            if path == "/api/v3/work_packages/60":
                return 200, wp
            return 404, {}

        with mock.patch.object(self.mod, "request_json", side_effect=fake_request), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_notifications_mark_resolved(args)
        self.assertEqual(p.call_args.args[0], 409)
        self.assertEqual(p.call_args.args[1]["error"], "status_condition_not_met")

    def test_report_daily_computes_summary(self):
        args = SimpleNamespace(project_id=7, since="2026-02-20", page_size=100, max_pages=2, limit=5)
        items = [
            {
                "id": 1,
                "createdAt": "2026-02-20T01:00:00Z",
                "updatedAt": "2026-02-20T02:00:00Z",
                "_links": {
                    "status": {"href": "/api/v3/statuses/2", "title": "In progress"},
                    "priority": {"href": "/api/v3/priorities/1", "title": "High"},
                },
            },
            {
                "id": 2,
                "createdAt": "2026-02-19T01:00:00Z",
                "updatedAt": "2026-02-20T03:00:00Z",
                "_links": {
                    "status": {"href": "/api/v3/statuses/3", "title": "Closed"},
                    "priority": {"href": "/api/v3/priorities/2", "title": "Low"},
                },
            },
        ]
        with mock.patch.object(self.mod, "_fetch_work_packages", return_value=(200, {"pages_fetched": 1}, items)), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_report_daily(args)
        printed = p.call_args.args[1]
        self.assertEqual(printed["created_count"], 1)
        self.assertEqual(printed["updated_count"], 2)
        self.assertEqual(printed["closed_count"], 1)
        self.assertEqual(printed["high_open_count"], 1)

    def test_report_assignee_filters_backlog_and_recent_updates(self):
        args = SimpleNamespace(project_id=7, assignee_id=42, since="2026-02-20", page_size=100, max_pages=2, limit=5)
        items = [
            {
                "id": 1,
                "createdAt": "2026-02-19T01:00:00Z",
                "updatedAt": "2026-02-20T01:00:00Z",
                "_links": {
                    "assignee": {"href": "/api/v3/users/42", "title": "A"},
                    "status": {"href": "/api/v3/statuses/2", "title": "In progress"},
                },
            },
            {
                "id": 2,
                "createdAt": "2026-02-19T01:00:00Z",
                "updatedAt": "2026-02-18T01:00:00Z",
                "_links": {
                    "assignee": {"href": "/api/v3/users/42", "title": "A"},
                    "status": {"href": "/api/v3/statuses/3", "title": "Closed"},
                },
            },
            {
                "id": 3,
                "createdAt": "2026-02-20T01:00:00Z",
                "updatedAt": "2026-02-20T01:00:00Z",
                "_links": {
                    "assignee": {"href": "/api/v3/users/99", "title": "B"},
                    "status": {"href": "/api/v3/statuses/2", "title": "In progress"},
                },
            },
        ]
        with mock.patch.object(self.mod, "_fetch_work_packages", return_value=(200, {"pages_fetched": 1}, items)), mock.patch.object(
            self.mod, "_print"
        ) as p:
            self.mod.cmd_report_assignee(args)
        printed = p.call_args.args[1]
        self.assertEqual(printed["assignee_id"], 42)
        self.assertEqual(printed["backlog_open_count"], 1)
        self.assertEqual(printed["updated_since_count"], 1)


@unittest.skipUnless(
    os.environ.get("OPENPROJECT_LIVE_TESTS") == "1",
    "Set OPENPROJECT_LIVE_TESTS=1 to run live OpenProject integration tests",
)
class OpenProjectLiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()
        cls.repo_root = pathlib.Path(__file__).resolve().parents[1]
        cls.script_path = cls.repo_root / "scripts" / "openproject_api.py"
        if not os.environ.get("OPENPROJECT_BASE_URL"):
            raise unittest.SkipTest("Missing OPENPROJECT_BASE_URL")
        if not os.environ.get("OPENPROJECT_API_KEY"):
            raise unittest.SkipTest("Missing OPENPROJECT_API_KEY")
        cls.demo_identifier = "demo-project"
        cls.demo_project_id = cls._resolve_demo_project_id()
        cls.type_id = cls._resolve_type_id()
        cls.me_id = cls._resolve_me_id()
        cls._assert_demo_membership_exists()

    @classmethod
    def _resolve_demo_project_id(cls) -> int:
        status, project = cls.mod.request_json("GET", f"/api/v3/projects/{cls.demo_identifier}")
        if status < 200 or status >= 300 or not isinstance(project, dict):
            raise unittest.SkipTest(f"Cannot resolve demo project '{cls.demo_identifier}' (status={status})")
        pid = project.get("id")
        ident = (project.get("identifier") or "").casefold()
        name = (project.get("name") or "").casefold()
        if not isinstance(pid, int):
            raise unittest.SkipTest("Resolved demo project has no numeric id")
        if ident != cls.demo_identifier and "demo" not in name:
            raise unittest.SkipTest("Resolved project is not demo scoped")
        return pid

    @classmethod
    def _resolve_me_id(cls) -> int:
        status, me = cls.mod.request_json("GET", "/api/v3/users/me")
        if status < 200 or status >= 300 or not isinstance(me, dict):
            raise unittest.SkipTest(f"Cannot resolve users/me (status={status})")
        uid = me.get("id")
        if not isinstance(uid, int):
            raise unittest.SkipTest("users/me response does not include numeric id")
        return uid

    @classmethod
    def _assert_demo_membership_exists(cls) -> None:
        status, data = cls.mod.request_json("GET", "/api/v3/memberships", params={"pageSize": 200})
        if status < 200 or status >= 300 or not isinstance(data, dict):
            raise unittest.SkipTest(f"Cannot inspect memberships (status={status})")
        found = False
        for m in cls.mod._collection_elements(data):
            links = m.get("_links", {}) if isinstance(m.get("_links"), dict) else {}
            principal = links.get("principal", {}) if isinstance(links.get("principal"), dict) else {}
            project = links.get("project", {}) if isinstance(links.get("project"), dict) else {}
            pid = cls.mod._href_tail_id(project.get("href")) if isinstance(project.get("href"), str) else None
            uid = cls.mod._href_tail_id(principal.get("href")) if isinstance(principal.get("href"), str) else None
            if uid == cls.me_id and pid == cls.demo_project_id:
                found = True
                break
        if not found:
            raise AssertionError(
                f"Live test requires membership for users/me={cls.me_id} in demo-project id={cls.demo_project_id}; none found."
            )

    @classmethod
    def _resolve_type_id(cls) -> int:
        status, data = cls.mod.request_json("GET", "/api/v3/types", params={"pageSize": 1})
        if status < 200 or status >= 300 or not isinstance(data, dict):
            raise unittest.SkipTest(f"Cannot resolve type id (status={status})")
        elements = cls.mod._collection_elements(data)
        if not elements or not isinstance(elements[0].get("id"), int):
            raise unittest.SkipTest("No type id available")
        return elements[0]["id"]

    @classmethod
    def _create_demo_work_package(cls, subject_suffix: str) -> int:
        stamp = int(time.time())
        subject = f"[codex-live-test] {subject_suffix}-{stamp}"
        create_payload = {
            "subject": subject,
            "_links": {
                "project": {"href": f"/api/v3/projects/{cls.demo_project_id}"},
                "type": {"href": f"/api/v3/types/{cls.type_id}"},
            },
        }
        st_create, created = cls.mod.request_json("POST", "/api/v3/work_packages", data=create_payload)
        if st_create in {401, 403}:
            raise unittest.SkipTest(f"Demo project mutation not permitted (status={st_create})")
        if not (200 <= st_create < 300):
            raise AssertionError(f"create failed: status={st_create}, data={created}")
        wp_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(wp_id, int):
            raise AssertionError(f"create returned invalid id payload: {created}")
        return wp_id

    @classmethod
    def _run_cli_json(cls, *args: str) -> dict:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            ["python3", str(cls.script_path), *args],
            cwd=str(cls.repo_root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"CLI returned non-zero exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return json.loads(proc.stdout)

    def test_live_01_create_and_context(self):
        wp_id = self._create_demo_work_package("create-context")
        payload = self._run_cli_json("wp-context", "--wp-id", str(wp_id))
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["data"]["work_package_id"], wp_id)

    def test_live_02_transition_and_update_by_name(self):
        wp_id = self._create_demo_work_package("transition-update")
        t = self._run_cli_json("wp-transition", "--wp-id", str(wp_id), "--to-status-name", "Review")
        if 200 <= t["status"] < 300:
            pass
        elif t["status"] == 422:
            body = t.get("data", {}).get("body", {})
            self.assertIn("PropertyConstraintViolation", body.get("errorIdentifier", ""))
        else:
            self.fail(f"unexpected transition status: {t}")
        u = self._run_cli_json("wp-update-by-name", "--wp-id", str(wp_id), "--priority-name", "Normal")
        self.assertTrue(200 <= u["status"] < 300, msg=u)

    def test_live_03_comment_and_activities_last(self):
        wp_id = self._create_demo_work_package("activities")
        comment_payload = {"comment": {"format": "markdown", "raw": "codex live integration test comment"}}
        st_comment, body = self.mod.request_json("POST", f"/api/v3/work_packages/{wp_id}/activities", data=comment_payload)
        self.assertTrue(200 <= st_comment < 300, msg=body)
        a = self._run_cli_json("wp-activities-last", "--wp-id", str(wp_id), "--count", "3")
        self.assertEqual(a["status"], 200)
        self.assertGreaterEqual(a["data"]["count"], 1)

    def test_live_04_resolvers(self):
        s = self._run_cli_json("statuses-resolve", "--name", "Review", "--exact")
        t = self._run_cli_json("types-resolve", "--name", "Task")
        p = self._run_cli_json("priorities-resolve", "--name", "Normal", "--exact")
        self.assertEqual(s["status"], 200)
        self.assertEqual(t["status"], 200)
        self.assertEqual(p["status"], 200)
        self.assertGreaterEqual(s["data"]["count"], 1)
        self.assertGreaterEqual(t["data"]["count"], 1)
        self.assertGreaterEqual(p["data"]["count"], 1)

    def test_live_05_notifications_and_reports(self):
        n = self._run_cli_json("notifications-last", "--count", "5", "--reason", "all")
        tr = self._run_cli_json("notifications-triage", "--count", "3", "--reason", "all")
        r = self._run_cli_json("report-daily", "--project-id", str(self.demo_project_id), "--limit", "5")
        self.assertEqual(n["status"], 200)
        self.assertEqual(tr["status"], 200)
        self.assertEqual(r["status"], 200)

    def test_live_06_finder_queues(self):
        pid = str(self.demo_project_id)
        f = self._run_cli_json(
            "wp-find", "--project-id", pid, "--status-name", "Review", "--page-size", "50", "--max-pages", "2"
        )
        my = self._run_cli_json("wp-list-my-open", "--project-id", pid, "--page-size", "20", "--max-pages", "2")
        due = self._run_cli_json(
            "wp-due-soon", "--project-id", pid, "--days", "30", "--page-size", "20", "--max-pages", "2"
        )
        stale = self._run_cli_json(
            "wp-stale", "--project-id", pid, "--inactive-days", "14", "--page-size", "20", "--max-pages", "2"
        )
        self.assertEqual(f["status"], 200)
        self.assertEqual(my["status"], 200)
        self.assertEqual(due["status"], 200)
        self.assertEqual(stale["status"], 200)

    def test_live_07_reference_lists_and_resolvers(self):
        pid = str(self.demo_project_id)
        s_list = self._run_cli_json("statuses-list")
        t_list = self._run_cli_json("types-list")
        p_list = self._run_cli_json("priorities-list")
        v_list = self._run_cli_json("versions-list", "--project-id", pid)
        v_resolve = self._run_cli_json("versions-resolve", "--project-id", pid, "--name", "version")
        self.assertEqual(s_list["status"], 200)
        self.assertEqual(t_list["status"], 200)
        self.assertEqual(p_list["status"], 200)
        self.assertEqual(v_list["status"], 200)
        self.assertEqual(v_resolve["status"], 200)

    def test_live_08_notifications_readonly_matrix(self):
        listed = self._run_cli_json("notifications-list", "--page-size", "10", "--reason", "all")
        unread_count = self._run_cli_json("notifications-unread-count", "--page-size", "10", "--max-pages", "2")
        mark_preview = self._run_cli_json("notifications-mark-all-read", "--dry-run", "--page-size", "10", "--max-pages", "2")
        self.assertEqual(listed["status"], 200)
        self.assertEqual(unread_count["status"], 200)
        self.assertEqual(mark_preview["status"], 200)
        elements = listed.get("data", {}).get("elements", [])
        if elements:
            nid = str(elements[0]["notification_id"])
            got = self._run_cli_json("notifications-get", "--notification-id", nid)
            tgt = self._run_cli_json("notifications-resolve-target", "--notification-id", nid)
            self.assertEqual(got["status"], 200)
            self.assertEqual(tgt["status"], 200)


if __name__ == "__main__":
    unittest.main()
