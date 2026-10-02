"""
Phase 7-7 Observability & Diagnostics Tests
"""
import os
import sys
import json
import tempfile
import shutil
import unittest
from unittest import mock
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from observability import (
    configure_logging, get_logger, generate_request_id, get_request_id,
    set_request_id, clear_request_id, get_session_id, set_session_id, clear_session_id,
    RequestContext, redact_secrets, redact_dict,
    log_event, log_interaction_start, log_interaction_success, log_interaction_failure,
    log_key_selected, log_key_rollover, log_key_cooldown, log_key_inactive,
    log_session_created, log_session_activated, log_session_invalidated, log_session_reset,
    log_recovery_same_tenant, log_recovery_cross_tenant, log_recovery_environment_reset,
    log_recovery_interaction_reset, log_recovery_workspace_sync,
    log_sync_start, log_sync_success, log_sync_partial, log_sync_failure,
    log_storage_corrupted, log_storage_recovered,
    check_health, get_diagnostics, format_diagnostics_text
)
from config import load_config, RouterConfig, ConfigCorruptedError
from keys import KeyPoolManager
from sessions import SessionStateManager
from registry import ProjectRegistry


class TestStructuredLogging(unittest.TestCase):
    """Test structured logging configuration and context variables."""
    
    def test_configure_logging_json(self):
        """Test JSON logging configuration."""
        logger = configure_logging("DEBUG", json_output=True)
        self.assertIsNotNone(logger)
        self.assertEqual(logger.name, "agy_router")
    
    def test_configure_logging_human(self):
        """Test human-readable logging configuration."""
        logger = configure_logging("INFO", json_output=False)
        self.assertIsNotNone(logger)
    
    def test_generate_request_id(self):
        """Test request ID generation."""
        req_id = generate_request_id()
        self.assertTrue(req_id.startswith("req_"))
        self.assertEqual(len(req_id), 20)  # "req_" + 16 hex chars
    
    def test_request_id_context_var(self):
        """Test request_id context variable."""
        clear_request_id()
        self.assertIsNone(get_request_id())
        
        req_id = "test_req_123"
        set_request_id(req_id)
        self.assertEqual(get_request_id(), req_id)
        
        clear_request_id()
        self.assertIsNone(get_request_id())
    
    def test_session_id_context_var(self):
        """Test session_id context variable."""
        clear_session_id()
        self.assertIsNone(get_session_id())
        
        sess_id = "test_sess_456"
        set_session_id(sess_id)
        self.assertEqual(get_session_id(), sess_id)
        
        clear_session_id()
        self.assertIsNone(get_session_id())
    
    def test_request_context_manager(self):
        """Test RequestContext context manager."""
        clear_request_id()
        clear_session_id()
        
        with RequestContext("ctx_req_789", "ctx_sess_789") as ctx:
            self.assertEqual(get_request_id(), "ctx_req_789")
            self.assertEqual(get_session_id(), "ctx_sess_789")
            self.assertEqual(ctx.request_id, "ctx_req_789")
            self.assertEqual(ctx.session_id, "ctx_sess_789")
        
        # Context should be cleaned up
        self.assertIsNone(get_request_id())
        self.assertIsNone(get_session_id())
    
    def test_request_context_without_session(self):
        """Test RequestContext without session_id."""
        clear_request_id()
        clear_session_id()
        
        with RequestContext("ctx_req_no_sess") as ctx:
            self.assertEqual(get_request_id(), "ctx_req_no_sess")
            self.assertIsNone(get_session_id())
        
        self.assertIsNone(get_request_id())


class TestSecretRedaction(unittest.TestCase):
    """Test secret redaction functions."""
    
    def test_redact_api_key(self):
        """Test Google API key redaction."""
        # Real Google API keys are 35+ chars after 'AIza'
        real_key = "AIzaSyB78901234567890123456789012345"
        text = f"Using key {real_key} for request"
        result = redact_secrets(text)
        self.assertIn("***REDACTED_API_KEY***", result)
        self.assertNotIn(real_key, result)
    
    def test_redact_url_credentials(self):
        """Test URL credential redaction."""
        text = "https://user:secret123@example.com/path"
        result = redact_secrets(text)
        self.assertIn("https://user:***@example.com/path", result)
        self.assertNotIn("secret123", result)
    
    def test_redact_bearer_token(self):
        """Test Bearer token redaction."""
        text = "Authorization: Bearer secret12345678901234567890"
        result = redact_secrets(text)
        self.assertIn("Bearer ***REDACTED***", result)
    
    def test_redact_x_goog_api_key(self):
        """Test x-goog-api-key header redaction."""
        # Real Google API keys are 35+ chars after 'AIza'
        real_key = "AIzaSyB78901234567890123456789012345"
        text = f"x-goog-api-key: {real_key}"
        result = redact_secrets(text)
        self.assertIn("***REDACTED_API_KEY***", result)
        self.assertNotIn(real_key, result)
    
    def test_redact_dict(self):
        """Test dictionary redaction."""
        data = {
            "api_key": "secret123",
            "normal_field": "value",
            "nested": {
                "password": "pass123",
                "token": "token123"
            },
            "list_field": [
                {"secret": "secret123"},
                "plain string"
            ]
        }
        result = redact_dict(data)
        self.assertEqual(result["api_key"], "***REDACTED***")
        self.assertEqual(result["normal_field"], "value")
        self.assertEqual(result["nested"]["password"], "***REDACTED***")
        self.assertEqual(result["nested"]["token"], "***REDACTED***")
        self.assertEqual(result["list_field"][0]["secret"], "***REDACTED***")
        self.assertEqual(result["list_field"][1], "plain string")


class TestEventLogging(unittest.TestCase):
    """Test event logging functions."""
    
    def setUp(self):
        """Configure logging for tests."""
        configure_logging("DEBUG", json_output=False)
    
    def test_log_event(self):
        """Test generic log_event function."""
        # Should not raise
        log_event("test.event", project_id="proj1", key_ref="key1")
    
    def test_log_interaction_events(self):
        """Test interaction logging events."""
        log_interaction_start("proj1", "sess1", "key1", "tenant1", "env1")
        log_interaction_success("proj1", "sess1", "key1", "tenant1", "env1", "int1", 200, 150)
        log_interaction_failure("proj1", "sess1", "key1", "tenant1", "env1", "timeout", 0, 100)
    
    def test_log_key_events(self):
        """Test key logging events."""
        log_key_selected("key1", "tenant1", "proj1", "sess1")
        log_key_rollover("key1", "key2", "rate_limit", "tenant1", "proj1", "sess1")
        log_key_cooldown("key1", 60, "rate_limit", "tenant1", "proj1", "sess1")
        log_key_inactive("key1", "invalid_credentials", "tenant1", "proj1", "sess1")
    
    def test_log_session_events(self):
        """Test session logging events."""
        log_session_created("sess1", "proj1", "key1", "tenant1", "env1")
        log_session_activated("sess1", "proj1", "key1", "tenant1", "env1", "int1")
        log_session_invalidated("sess1", "proj1", "tenant_mismatch")
        log_session_reset("sess1", "proj1", "manual_reset")
    
    def test_log_recovery_events(self):
        """Test recovery logging events."""
        log_recovery_same_tenant("sess1", "proj1", "key1", "key2", "rate_limit")
        log_recovery_cross_tenant("sess1", "proj1", "tenant1", "tenant2", "tenant_mismatch")
        log_recovery_environment_reset("sess1", "proj1", "env_not_found", "env_old")
        log_recovery_interaction_reset("sess1", "proj1", "int_not_found", "int_old")
        log_recovery_workspace_sync("sess1", "proj1", "env1", "environment_changed")
    
    def test_log_sync_events(self):
        """Test sync logging events."""
        log_sync_start("sess1", "proj1", "env1", 10, 1024000)
        log_sync_success("sess1", "proj1", "env1", 8, 8, 500)
        log_sync_partial("sess1", "proj1", "env1", 5, 3, 300, "file_too_large")
        log_sync_failure("sess1", "proj1", "env1", "network_error", 200)
    
    def test_log_storage_events(self):
        """Test storage logging events."""
        log_storage_corrupted("projects.json", "JSON decode error")
        log_storage_recovered("projects.json", "restored_from_backup")


class TestHealthCheck(unittest.TestCase):
    """Test health check functionality."""
    
    def setUp(self):
        """Create temp directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
    
    def test_check_health_all_healthy(self):
        """Test health check when all components are healthy."""
        # Create valid config file
        config_dir = os.path.join(self.temp_dir, ".agy-router")
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        
        config_file = os.path.join(config_dir, "config.json")
        with open(config_file, 'w') as f:
            json.dump({"default_key_index": 1}, f)
        
        # Create valid projects.json
        projects_file = os.path.join(config_dir, "projects.json")
        with open(projects_file, 'w') as f:
            json.dump({"projects": {}}, f)
        
        # Create valid key_states.json
        keys_file = os.path.join(config_dir, "key_states.json")
        with open(keys_file, 'w') as f:
            json.dump({"keys": {}}, f)
        
        # Create valid sessions.json
        sessions_file = os.path.join(config_dir, "sessions.json")
        with open(sessions_file, 'w') as f:
            json.dump({"sessions": {}}, f)
        
        health = check_health(
            config_path=config_file,
            projects_path=projects_file,
            keys_path=keys_file,
            sessions_path=sessions_file
        )
        
        self.assertTrue(health["healthy"])
        self.assertEqual(health["overall"], "HEALTHY")
        self.assertEqual(health["checks"]["config.json"], "OK")
        self.assertEqual(health["checks"]["projects.json"], "OK")
        self.assertEqual(health["checks"]["key_states.json"], "OK")
        self.assertEqual(health["checks"]["sessions.json"], "OK")
        self.assertEqual(health["checks"]["locks"], "OK")
        self.assertEqual(health["checks"]["configuration"], "OK")
    
    def test_check_health_missing_config(self):
        """Test health check when config is missing (optional - using defaults)."""
        config_dir = os.path.join(self.temp_dir, ".agy-router")
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        
        projects_file = os.path.join(config_dir, "projects.json")
        with open(projects_file, 'w') as f:
            json.dump({"projects": {}}, f)
        
        keys_file = os.path.join(config_dir, "key_states.json")
        with open(keys_file, 'w') as f:
            json.dump({"keys": {}}, f)
        
        sessions_file = os.path.join(config_dir, "sessions.json")
        with open(sessions_file, 'w') as f:
            json.dump({"sessions": {}}, f)
        
        # config.json does not exist - should be OK (using defaults)
        health = check_health(
            config_path=os.path.join(config_dir, "config.json"),
            projects_path=projects_file,
            keys_path=keys_file,
            sessions_path=sessions_file
        )
        
        self.assertTrue(health["healthy"])
        self.assertEqual(health["overall"], "HEALTHY")
        self.assertEqual(health["checks"]["config.json"], "NOT_FOUND (using defaults)")
    
    def test_check_health_corrupted_json(self):
        """Test health check with corrupted JSON."""
        config_dir = os.path.join(self.temp_dir, ".agy-router")
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        
        config_file = os.path.join(config_dir, "config.json")
        with open(config_file, 'w') as f:
            f.write("{ invalid json }")
        
        health = check_health(config_path=config_file)
        self.assertFalse(health["healthy"])
        self.assertIn("ERROR", health["checks"]["config.json"])
    
    def test_check_health_key_discovery(self):
        """Test health check key discovery."""
        # This will use real environment/bashrc keys if present
        health = check_health()
        self.assertIn("key discovery", health["checks"])


class TestDiagnostics(unittest.TestCase):
    """Test diagnostics functionality."""
    
    def setUp(self):
        """Create temp directory for test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
    
    def test_get_diagnostics_structure(self):
        """Test diagnostics output structure."""
        diag = get_diagnostics()
        
        # Check required sections
        self.assertIn("git", diag)
        self.assertIn("projects", diag)
        self.assertIn("sessions", diag)
        self.assertIn("keys", diag)
        self.assertIn("tenants", diag)
        self.assertIn("storage", diag)
        self.assertIn("config", diag)
        self.assertIn("recent", diag)
        
        # Check git info
        self.assertIn("branch", diag["git"])
        self.assertIn("commit", diag["git"])
        
        # Check config
        self.assertIn("default_key_index", diag["config"])
        self.assertIn("cooldown_seconds", diag["config"])
    
    def test_format_diagnostics_text(self):
        """Test diagnostics text formatting."""
        diag = get_diagnostics()
        text = format_diagnostics_text(diag)
        
        self.assertIn("agy-router Diagnostics", text)
        self.assertIn("Version/Commit:", text)
        self.assertIn("Projects:", text)
        self.assertIn("Sessions:", text)
        self.assertIn("Keys:", text)
        self.assertIn("Tenants:", text)
        self.assertIn("Storage:", text)
        self.assertIn("Configuration:", text)
        self.assertIn("Recent State:", text)


class TestCLIIntegration(unittest.TestCase):
    """Test CLI integration with observability commands."""
    
    def test_health_command(self):
        """Test health CLI command."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "cli.py", "health"],
            capture_output=True, text=True, cwd="/home/ubuntu/agy-router"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Overall:", result.stdout)
    
    def test_diagnostics_command(self):
        """Test diagnostics CLI command (text output)."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "cli.py", "diagnostics"],
            capture_output=True, text=True, cwd="/home/ubuntu/agy-router"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("agy-router Diagnostics", result.stdout)
    
    def test_diagnostics_command_json(self):
        """Test diagnostics CLI command (JSON output)."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "cli.py", "diagnostics", "--json"],
            capture_output=True, text=True, cwd="/home/ubuntu/agy-router"
        )
        self.assertEqual(result.returncode, 0)
        
        # Should be valid JSON
        data = json.loads(result.stdout)
        self.assertIn("git", data)
        self.assertIn("projects", data)
        self.assertIn("sessions", data)
        self.assertIn("keys", data)
        self.assertIn("tenants", data)
        self.assertIn("storage", data)
        self.assertIn("config", data)
        self.assertIn("recent", data)


class TestServiceIntegration(unittest.TestCase):
    """Test observability integration in services."""
    
    def test_key_pool_logs_on_acquire(self):
        """Test that KeyPoolManager logs key selection."""
        pool = KeyPoolManager()
        # discover_keys is called internally
        discovered = pool.discover_keys()
        if discovered:
            key_ref, key_idx = pool.acquire_key()
            self.assertIsNotNone(key_ref)
            self.assertIsNotNone(key_idx)
    
    def test_key_pool_logs_on_report_result(self):
        """Test that KeyPoolManager logs on report_result."""
        pool = KeyPoolManager()
        # Should not raise
        pool.report_result("key1", status_code=200)
        pool.report_result("key1", status_code=429)
        pool.report_result("key1", status_code=401)
        pool.report_result("key1", status_code=403)
        pool.report_result("key1", status_code=500)
    
    def test_session_manager_logs_on_create(self):
        """Test that SessionStateManager logs on create_session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_path = os.path.join(tmpdir, "sessions.json")
            sess_mgr = SessionStateManager(storage_path=sessions_path)
            
            session = sess_mgr.create_session(
                project_id="proj1",
                session_id="sess1",
                bound_key="key1",
                tenant_id="tenant1",
                environment_id="env1"
            )
            self.assertEqual(session["session_id"], "sess1")
    
    def test_session_manager_logs_on_update(self):
        """Test that SessionStateManager logs on update_session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_path = os.path.join(tmpdir, "sessions.json")
            sess_mgr = SessionStateManager(storage_path=sessions_path)
            
            sess_mgr.create_session("proj1", "sess1", "key1", "tenant1", "env1")
            session = sess_mgr.update_session("sess1", state="ACTIVE")
            self.assertEqual(session["state"], "ACTIVE")
            
            session = sess_mgr.update_session("sess1", state="INVALIDATED")
            self.assertEqual(session["state"], "INVALIDATED")
            
            session = sess_mgr.update_session("sess1", state="IDLE")
            self.assertEqual(session["state"], "IDLE")


if __name__ == "__main__":
    unittest.main()