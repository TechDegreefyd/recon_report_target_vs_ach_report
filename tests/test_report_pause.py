"""Exercise report orchestration with all external services mocked."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apscheduler.schedulers.blocking import BlockingScheduler


ROOT = Path(__file__).resolve().parents[1]


def load_script(filename):
    path = ROOT / filename
    namespace = {"__name__": "report_pause_audit", "__file__": str(path)}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace


class ReportPauseTests(unittest.TestCase):
    def test_scheduler_and_startup_paths(self):
        for smoke_enabled in ("0", "1"):
            with self.subTest(smoke=smoke_enabled), patch.dict(
                os.environ, {"SMOKE_TEST": smoke_enabled}, clear=True
            ), patch("dotenv.load_dotenv"), patch("builtins.print"):
                ns = load_script("server.py")
                scheduler = BlockingScheduler(timezone=ns["pytz"].UTC)
                scheduler.start = Mock()
                ns["BlockingScheduler"] = Mock(return_value=scheduler)
                runner = Mock()
                ns["run_script"] = runner
                # Run the smoke-test callback synchronously, with no threads.
                with patch("threading.Thread") as thread:
                    thread.side_effect = lambda target, **kw: types.SimpleNamespace(start=target)
                    ns["main"]()
                jobs = scheduler.get_jobs()
                expected = sum(len(ns[name]) for name in (
                    "SCHEDULE", "CALLBACK_SCHEDULE", "SERVICING_SCHEDULE",
                    "MARKETING_HUB_SCHEDULE", "BLOG_SCHEDULE", "BLOG_DEEP_SCHEDULE",
                )) + 1  # cleanup
                self.assertEqual(len(jobs), expected)
                report_jobs = [job for job in jobs if job.func is runner]
                self.assertFalse(any("recon" in job.args[0] for job in report_jobs))
                lms_jobs = [job for job in report_jobs if job.args[0] == "generate_all_lms_reports.py"]
                self.assertEqual(len(lms_jobs), 2)
                for job in lms_jobs:
                    self.assertIn("--online-only", job.args[2]())
                self.assertTrue(any("--last-activity-only" in job.args[2]() for job in lms_jobs))
                self.assertTrue(any("--skip-last-activity" in job.args[2]() for job in lms_jobs))
                self.assertEqual({job.args[0] for job in report_jobs}, {
                    "generate_all_lms_reports.py", "tat_reports.py",
                    "generate_callback_reports.py", "generate_servicing_report.py",
                    "generate_marketing_hub_ingest.py", os.path.join("META", "daily_ad_alert.py"),
                    os.path.join("META", "daily_blog_alert.py"),
                })
                if smoke_enabled == "1":
                    self.assertEqual(runner.call_count, 2)
                    for call in runner.call_args_list:
                        self.assertNotIn("recon", call.args[0])
                        if call.args[0] == "generate_all_lms_reports.py":
                            self.assertIn("--online-only", call.args[2]())
                else:
                    runner.assert_not_called()
                scheduler.start.assert_called_once()

    def test_online_runs_never_touch_regular_workflows(self):
        cases = (
            (["--online-only", "--yesterday", "--last-activity-only"], ["t6"], False),
            (["--online-only", "--skip-last-activity"], ["t1", "t4", "t5", "t7", "t8"], False),
            (["--online-only"], ["t1", "t4", "t5", "t6", "t7", "t8"], False),
            (["--last-activity-only"], ["t6"], False),
            (["--online-only"], [], True),
        )
        for flags, expected_tabs, online_fails in cases:
            with self.subTest(flags=flags, online_fails=online_fails), tempfile.TemporaryDirectory(
                dir=ROOT
            ) as workspace, contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    "WORKSPACE_DIR": workspace,
                    "REGULAR_LMS_DB_PORT": "paused-invalid-port",
                    "REGULAR_AMITY_LMS_DB_PORT": "paused-invalid-port",
                }, clear=True))
                stack.enter_context(patch.object(sys, "argv", ["generate_all_lms_reports.py", "--nocleanup", *flags]))
                stack.enter_context(patch("dotenv.load_dotenv"))
                stack.enter_context(patch("builtins.print"))
                stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP forbidden")))
                connection = stack.enter_context(patch("asyncpg.connect", new_callable=AsyncMock))
                sheets = types.ModuleType("sheets_config")
                for name in ("load_online_config", "load_regular_config", "log_report", "get_service",
                             "sync_online_counsellors", "sync_regular_dates", "_norm"):
                    setattr(sheets, name, Mock())
                sheets.load_online_config.return_value = {}
                sheets.load_regular_config.side_effect = AssertionError("Regular config forbidden")
                stack.enter_context(patch.dict(sys.modules, {"sheets_config": sheets}))
                ns = load_script("generate_all_lms_reports.py")
                sheets.load_regular_config.assert_not_called()
                self.assertTrue(ns["SKIP_REGULAR"])
                regular_mocks = []
                for name in ("regular_get_data", "regular_prepare_data", "regular_generate_html",
                             "amity_get_total_forms_last_year", "amity_get_total_forms_this_year",
                             "amity_get_admissions_last_year", "amity_get_admissions_this_year"):
                    mock = Mock(side_effect=AssertionError("Regular workflow forbidden"))
                    ns[name] = mock
                    regular_mocks.append(mock)
                ns["online_get_data"] = AsyncMock(return_value=(None,) * 11)
                if online_fails:
                    ns["online_get_data"].side_effect = RuntimeError("Simulated Online DB failure")
                ns["online_prepare_data"] = Mock(return_value={})
                ns["online_generate_html"] = Mock(return_value=("online.html", "online summary"))

                async def screenshots(html_path, tabs, paths, **kwargs):
                    for path in paths:
                        Path(path).write_bytes(b"mock screenshot")
                    return [True] * len(paths)

                ns["screenshot_html_tabs"] = AsyncMock(side_effect=screenshots)
                ns["send_via_whapi"] = Mock(return_value=True)
                with contextlib.redirect_stdout(io.StringIO()):
                    asyncio.run(ns["main"]())
                for mock in regular_mocks:
                    mock.assert_not_called()
                sheets.sync_regular_dates.assert_not_called()
                connection.assert_not_called()
                sheets.log_report.assert_called_once()
                self.assertEqual(sheets.log_report.call_args.args[1], "Online LMS")
                if expected_tabs:
                    self.assertEqual(ns["screenshot_html_tabs"].call_args.args[1], expected_tabs)
                    self.assertGreater(ns["send_via_whapi"].call_count, 0)
                else:
                    ns["screenshot_html_tabs"].assert_not_called()
                    ns["send_via_whapi"].assert_not_called()
                for call in ns["send_via_whapi"].call_args_list:
                    self.assertTrue(Path(call.args[0]).name.startswith("Online_LMS_"))
                manifests = list(Path(ns["OUTPUT_DIR"]).glob("delivery_manifest_*.json"))
                self.assertEqual(len(manifests), 1)
                files = json.loads(manifests[0].read_text())["files"]
                self.assertEqual(len(files), len(expected_tabs))
                self.assertTrue(all(item["filename"].startswith("Online_LMS_") for item in files))


if __name__ == "__main__":
    unittest.main()
