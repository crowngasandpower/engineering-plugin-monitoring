"""
Unit tests for onprem/exemption-api/main.py.

Run: pip install -r requirements-test.txt && pytest test_main.py -v

No real database or network is needed — all external calls are mocked.
FastAPI endpoint functions are called directly as plain Python functions,
which works because Query(...) default values are only used by the ASGI
dispatch layer; explicit kwargs bypass them.
"""
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_conn(rows=None):
    """Return a drop-in replacement for main.get_conn that yields a mock conn.

    Usage: patch("main.get_conn", _mock_conn(rows=[("host:9100",)]))
    """
    @contextmanager
    def _ctx():
        conn = MagicMock()
        cur = MagicMock()
        if rows is not None:
            cur.fetchall.return_value = rows
        conn.cursor.return_value = cur
        yield conn
    return _ctx


def _urlopen_resp(body: str):
    """Return a mock urllib response context manager for the given body text."""
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    m.read.return_value = body.encode()
    return m


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_caches():
    """Wipe in-process caches before and after every test."""
    main._node_memory_exempt_cache = None
    main._vm_powered_off_exempt_cache = None
    yield
    main._node_memory_exempt_cache = None
    main._vm_powered_off_exempt_cache = None


# ---------------------------------------------------------------------------
# _SAFE_IDENTIFIER_RE / _SAFE_NAME_RE — input-allowlist boundary cases
# ---------------------------------------------------------------------------

class TestSafeRegexes:
    """_SAFE_IDENTIFIER_RE (allows colons) vs _SAFE_NAME_RE (rejects colons)."""

    def test_identifier_re_accepts_hostname_with_port(self):
        assert main._SAFE_IDENTIFIER_RE.match("apps-prod-mysql:9100")

    def test_identifier_re_accepts_ip_with_port(self):
        assert main._SAFE_IDENTIFIER_RE.match("192.168.1.100:9100")

    def test_identifier_re_accepts_dots_and_hyphens(self):
        assert main._SAFE_IDENTIFIER_RE.match("apps.prod-mysql:9100")

    def test_identifier_re_rejects_empty_string(self):
        assert not main._SAFE_IDENTIFIER_RE.match("")

    @pytest.mark.parametrize("char", list("()[]{}^$*+?|\\"))
    def test_identifier_re_rejects_promql_metacharacters(self, char):
        assert not main._SAFE_IDENTIFIER_RE.match(f"host{char}name")

    def test_name_re_rejects_colon(self):
        assert not main._SAFE_NAME_RE.match("vm-name:port")

    def test_name_re_accepts_plain_vm_name(self):
        assert main._SAFE_NAME_RE.match("some-vm-prod-01")

    def test_name_re_accepts_dots(self):
        assert main._SAFE_NAME_RE.match("some.vm.name")

    def test_name_re_rejects_empty_string(self):
        assert not main._SAFE_NAME_RE.match("")

    # Category-aware routing: instance-label categories allow colons; VM-name categories do not.

    def test_suppress_add_allows_colon_for_high_memory_host(self):
        """high_memory_host is an instance-label category — colon must be accepted."""
        with patch("main.get_conn", _mock_conn()):
            result = main.suppress_add(
                category="high_memory_host",
                identifier="apps-prod-mysql:9100",
                reason="db server",
            )
        assert result is not None

    def test_suppress_add_rejects_colon_for_powered_off(self):
        """powered_off is a VM-name category — colon must be rejected."""
        with pytest.raises(HTTPException) as exc_info:
            main.suppress_add(category="powered_off", identifier="vm-name:bad", reason="")
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize("char", list("()[]{}^$*+?|\\"))
    def test_suppress_add_rejects_promql_metacharacters_in_identifier(self, char):
        with pytest.raises(HTTPException) as exc_info:
            main.suppress_add(category="powered_off", identifier=f"vm{char}name", reason="")
        assert exc_info.value.status_code == 400

    def test_suppress_remove_rejects_colon_for_vm_name_category(self):
        with pytest.raises(HTTPException) as exc_info:
            main.suppress_remove(category="powered_off", identifier="vm-name:bad")
        assert exc_info.value.status_code == 400

    def test_suppress_add_rejects_reason_over_255_chars(self):
        with pytest.raises(HTTPException) as exc_info:
            main.suppress_add(
                category="powered_off",
                identifier="some-vm",
                reason="x" * 256,
            )
        assert exc_info.value.status_code == 400

    def test_exempt_add_rejects_metacharacter_in_host(self):
        with pytest.raises(HTTPException) as exc_info:
            main.add_exempt(host="host(name)", reason="")
        assert exc_info.value.status_code == 400

    def test_exempt_add_accepts_valid_hostname_port(self):
        with patch("main.get_conn", _mock_conn()):
            result = main.add_exempt(host="apps-prod-mysql:9100", reason="test")
        assert result is not None

    def test_exempt_add_rejects_reason_over_255_chars(self):
        with pytest.raises(HTTPException) as exc_info:
            main.add_exempt(host="host:9100", reason="x" * 256)
        assert exc_info.value.status_code == 400

    def test_exempt_remove_rejects_metacharacter_in_host(self):
        with pytest.raises(HTTPException) as exc_info:
            main.remove_exempt(host="host[name]")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# node_memory_exempt_metrics — stale-cache fallback and DB integration
# ---------------------------------------------------------------------------

class TestNodeMemoryExemptCache:
    """Cache semantics for /node-memory-exempt/metrics."""

    def test_serves_warm_cache_without_hitting_db(self):
        cached_body = '# TYPE node_memory_exempt gauge\nnode_memory_exempt{instance="x:9100"} 1\n'
        main._node_memory_exempt_cache = (time.monotonic(), cached_body)

        with patch("main.get_conn") as mock_conn:
            resp = main.node_memory_exempt_metrics()
            mock_conn.assert_not_called()

        assert resp.body == cached_body.encode()

    def test_returns_stale_body_when_db_raises_on_cache_expiry(self):
        """An expired cache entry must be returned as a fallback when the DB fails."""
        stale_body = 'node_memory_exempt{instance="apps-prod-mysql:9100"} 1\n'
        main._node_memory_exempt_cache = (
            time.monotonic() - main._NODE_MEMORY_EXEMPT_TTL - 1,
            stale_body,
        )

        with patch("main.get_conn", side_effect=Exception("DB down")):
            resp = main.node_memory_exempt_metrics()

        assert resp.body == stale_body.encode()

    def test_returns_empty_gauge_header_when_no_cache_and_db_fails(self):
        """First-startup DB failure with no prior cache: return header-only HTTP 200."""
        assert main._node_memory_exempt_cache is None

        with patch("main.get_conn", side_effect=Exception("DB down")):
            resp = main.node_memory_exempt_metrics()

        body = resp.body.decode()
        assert "# HELP node_memory_exempt" in body
        assert "# TYPE node_memory_exempt gauge" in body
        assert "} 1" not in body  # no series emitted

    def test_builds_gauge_lines_from_db_rows(self):
        rows = [("apps-prod-mysql:9100",), ("apps-prod-mssql:9182",)]
        with patch("main.get_conn", _mock_conn(rows=rows)):
            resp = main.node_memory_exempt_metrics()

        body = resp.body.decode()
        assert 'node_memory_exempt{instance="apps-prod-mysql:9100"} 1' in body
        assert 'node_memory_exempt{instance="apps-prod-mssql:9182"} 1' in body

    def test_escapes_double_quote_in_db_identifier(self):
        """A quote in a DB identifier must be escaped so Prometheus text format is valid."""
        rows = [('host"injected":9100',)]
        with patch("main.get_conn", _mock_conn(rows=rows)):
            resp = main.node_memory_exempt_metrics()

        body = resp.body.decode()
        # Unescaped quote must not appear inside the label value
        assert 'instance="host"injected"' not in body
        assert '\\"injected\\"' in body


# ---------------------------------------------------------------------------
# vm_powered_off_exempt_metrics — cache semantics (mirrors node_memory tests)
# ---------------------------------------------------------------------------

class TestVmPoweredOffExemptCache:
    """Cache semantics for /vm-powered-off-exempt/metrics."""

    def test_serves_warm_cache_without_hitting_db(self):
        cached_body = '# TYPE vcenter_vm_powered_off_exempt gauge\nvcenter_vm_powered_off_exempt{vm_name="prod-db-01"} 1\n'
        main._vm_powered_off_exempt_cache = (time.monotonic(), cached_body)

        with patch("main.get_conn") as mock_conn:
            resp = main.vm_powered_off_exempt_metrics()
            mock_conn.assert_not_called()

        assert resp.body == cached_body.encode()

    def test_returns_stale_body_when_db_raises_on_cache_expiry(self):
        stale_body = 'vcenter_vm_powered_off_exempt{vm_name="prod-db-01"} 1\n'
        main._vm_powered_off_exempt_cache = (
            time.monotonic() - main._VM_POWERED_OFF_EXEMPT_TTL - 1,
            stale_body,
        )

        with patch("main.get_conn", side_effect=Exception("DB down")):
            resp = main.vm_powered_off_exempt_metrics()

        assert resp.body == stale_body.encode()

    def test_returns_empty_gauge_header_when_no_cache_and_db_fails(self):
        assert main._vm_powered_off_exempt_cache is None

        with patch("main.get_conn", side_effect=Exception("DB down")):
            resp = main.vm_powered_off_exempt_metrics()

        body = resp.body.decode()
        assert "# HELP vcenter_vm_powered_off_exempt" in body
        assert "# TYPE vcenter_vm_powered_off_exempt gauge" in body
        assert "} 1" not in body

    def test_builds_gauge_lines_from_db_rows(self):
        rows = [("prod-db-01",), ("prod-db-02",)]
        with patch("main.get_conn", _mock_conn(rows=rows)):
            resp = main.vm_powered_off_exempt_metrics()

        body = resp.body.decode()
        assert 'vcenter_vm_powered_off_exempt{vm_name="prod-db-01"} 1' in body
        assert 'vcenter_vm_powered_off_exempt{vm_name="prod-db-02"} 1' in body

    def test_escapes_double_quote_in_vm_name(self):
        rows = [('vm"injected"name',)]
        with patch("main.get_conn", _mock_conn(rows=rows)):
            resp = main.vm_powered_off_exempt_metrics()

        body = resp.body.decode()
        assert 'vm_name="vm"injected"' not in body
        assert '\\"injected\\"' in body


# ---------------------------------------------------------------------------
# _get_unifi_sites — parsing, comment skipping, deduplication
# ---------------------------------------------------------------------------

class TestGetUnifiSites:
    """_get_unifi_sites merges both exporters and must skip # comment lines."""

    def test_parses_unifi_host_up_from_site_manager_exporter(self):
        body = (
            "# HELP unifi_host_up Whether the host is up\n"
            "# TYPE unifi_host_up gauge\n"
            'unifi_host_up{host="Site A"} 1\n'
            'unifi_host_up{host="Site B"} 0\n'
        )
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [Exception("local down"), _urlopen_resp(body)]
            sites = main._get_unifi_sites()

        assert "Site A" in sites
        assert "Site B" in sites

    def test_skips_comment_lines_that_contain_metric_names(self):
        """Lines starting with # must never be parsed as metric data."""
        body = (
            "# unifi_host_up{host=\"ShouldBeSkipped\"} 1\n"
            'unifi_host_up{host="RealSite"} 1\n'
        )
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [Exception("local down"), _urlopen_resp(body)]
            sites = main._get_unifi_sites()

        assert "RealSite" in sites
        assert "ShouldBeSkipped" not in sites

    def test_merges_sites_from_local_and_site_manager_exporters(self):
        local_body = 'unifi_scrape_success{site="LocalOnly"} 1\n'
        sm_body = 'unifi_host_up{host="SMOnly"} 1\n'

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [_urlopen_resp(local_body), _urlopen_resp(sm_body)]
            sites = main._get_unifi_sites()

        assert "LocalOnly" in sites
        assert "SMOnly" in sites

    def test_deduplicates_sites_present_in_both_exporters(self):
        body_local = 'unifi_scrape_success{site="SharedSite"} 1\n'
        body_sm = 'unifi_host_up{host="SharedSite"} 1\n'

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [_urlopen_resp(body_local), _urlopen_resp(body_sm)]
            sites = main._get_unifi_sites()

        assert sites.count("SharedSite") == 1

    def test_returns_sorted_list(self):
        body = 'unifi_host_up{host="Zulu"} 1\nunifi_host_up{host="Alpha"} 1\n'

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [Exception("local down"), _urlopen_resp(body)]
            sites = main._get_unifi_sites()

        assert sites == sorted(sites)

    def test_returns_empty_list_when_both_exporters_unavailable(self):
        with patch("urllib.request.urlopen", side_effect=Exception("all down")):
            sites = main._get_unifi_sites()

        assert sites == []
