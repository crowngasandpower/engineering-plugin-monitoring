"""
Unit tests for the PagerDuty maintenance-window paths in monitor.py.

Run: pip install -r requirements-test.txt && pytest test_monitor.py -v

No real network is needed — monitor.requests is patched in every test.
Importing monitor is side-effect free (the tray only runs under __main__;
settings.load() is guarded), so the functions are called directly.
"""
from unittest.mock import MagicMock, patch

import monitor


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

PD_PROFILE = {
    "source_type":   "pagerduty",
    "pd_api_key":    "tok123",
    "pd_user_email": "john@example.com",
    "pd_service_id": "PSVC1",
    "grafana_url":   "http://grafana.example",
}


def _resp(status=200, json_data=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data if json_data is not None else {}
    m.text = text
    return m


# ---------------------------------------------------------------------------
# _create_pd_maintenance_window
# ---------------------------------------------------------------------------

class TestCreatePdMaintenanceWindow:
    def test_create_success_posts_to_pd_with_service(self):
        with patch.object(monitor, "requests") as rq:
            rq.post.return_value = _resp(201)
            ok, msg = monitor._create_pd_maintenance_window(1, PD_PROFILE)
        assert (ok, msg) == (True, "Maintenance window created")
        assert rq.post.call_args.args[0] == "https://api.pagerduty.com/maintenance_windows"
        window = rq.post.call_args.kwargs["json"]["maintenance_window"]
        assert window["services"] == [{"id": "PSVC1", "type": "service_reference"}]
        assert window["description"] == "[MAINTENANCE] Crown maintenance window (1h)"

    def test_missing_service_id_returns_error_and_no_request(self):
        prof = {**PD_PROFILE, "pd_service_id": ""}
        with patch.object(monitor, "requests") as rq:
            ok, msg = monitor._create_pd_maintenance_window(1, prof)
        assert (ok, msg) == (False, "No PagerDuty service ID set on this profile")
        rq.post.assert_not_called()

    def test_missing_user_email_returns_error_and_no_request(self):
        prof = {**PD_PROFILE, "pd_user_email": ""}
        with patch.object(monitor, "requests") as rq:
            ok, msg = monitor._create_pd_maintenance_window(1, prof)
        assert (ok, msg) == (False, "No PagerDuty user email set on this profile")
        rq.post.assert_not_called()

    def test_http_error_is_surfaced(self):
        with patch.object(monitor, "requests") as rq:
            rq.post.return_value = _resp(403, text="Forbidden")
            ok, msg = monitor._create_pd_maintenance_window(1, PD_PROFILE)
        assert ok is False
        assert msg.startswith("HTTP 403")


# ---------------------------------------------------------------------------
# _end_pd_maintenance_window
# ---------------------------------------------------------------------------

class TestEndPdMaintenanceWindow:
    def test_end_success(self):
        with patch.object(monitor, "requests") as rq:
            rq.delete.return_value = _resp(204)
            ok, msg = monitor._end_pd_maintenance_window("MW1", PD_PROFILE)
        assert (ok, msg) == (True, "Maintenance ended")
        assert rq.delete.call_args.args[0] == "https://api.pagerduty.com/maintenance_windows/MW1"

    def test_blank_id_guard_makes_no_request(self):
        with patch.object(monitor, "requests") as rq:
            ok, msg = monitor._end_pd_maintenance_window("   ", PD_PROFILE)
        assert (ok, msg) == (False, "No maintenance window ID provided")
        rq.delete.assert_not_called()


# ---------------------------------------------------------------------------
# _fetch_pd_maintenance
# ---------------------------------------------------------------------------

class TestFetchPdMaintenance:
    def test_none_when_no_maintenance_tagged_window(self):
        with patch.object(monitor, "requests") as rq:
            rq.get.return_value = _resp(200, {"maintenance_windows": [
                {"id": "X", "end_time": "2026-07-01T12:00:00Z",
                 "description": "unrelated window"},
            ]})
            assert monitor._fetch_pd_maintenance(PD_PROFILE) is None

    def test_returns_normalised_window(self):
        with patch.object(monitor, "requests") as rq:
            rq.get.return_value = _resp(200, {"maintenance_windows": [
                {"id": "MW9", "end_time": "2026-07-01T12:00:00Z",
                 "description": "[MAINTENANCE] Crown maintenance window (1h)"},
            ]})
            assert monitor._fetch_pd_maintenance(PD_PROFILE) == {
                "id": "MW9", "endsAt": "2026-07-01T12:00:00Z"}

    def test_none_without_service_id_makes_no_request(self):
        prof = {**PD_PROFILE, "pd_service_id": ""}
        with patch.object(monitor, "requests") as rq:
            assert monitor._fetch_pd_maintenance(prof) is None
        rq.get.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch — the original bug: PD profiles must not hit the Grafana API
# ---------------------------------------------------------------------------

class TestMaintenanceDispatch:
    def test_create_on_pd_profile_dispatches_to_pd_not_grafana(self):
        with patch.object(monitor, "_create_pd_maintenance_window",
                           return_value=(True, "ok")) as pd, \
             patch.object(monitor, "requests") as rq:
            ok, msg = monitor._create_maintenance_silence(1, PD_PROFILE)
        pd.assert_called_once_with(1, PD_PROFILE)
        rq.post.assert_not_called()          # did NOT touch Grafana silences
        assert (ok, msg) == (True, "ok")

    def test_end_on_pd_profile_dispatches_to_pd_not_grafana(self):
        with patch.object(monitor, "_end_pd_maintenance_window",
                           return_value=(True, "ok")) as pd, \
             patch.object(monitor, "requests") as rq:
            monitor._end_maintenance_silence("MW1", PD_PROFILE)
        pd.assert_called_once_with("MW1", PD_PROFILE)
        rq.delete.assert_not_called()

    def test_create_on_grafana_profile_hits_grafana(self):
        prof = {"source_type": "grafana", "grafana_url": "http://g",
                "auth_type": "token", "api_token": "t"}
        with patch.object(monitor, "_create_pd_maintenance_window") as pd, \
             patch.object(monitor, "requests") as rq:
            rq.post.return_value = _resp(201)
            monitor._create_maintenance_silence(1, prof)
        pd.assert_not_called()
        rq.post.assert_called_once()
        assert "alertmanager" in rq.post.call_args.args[0]
