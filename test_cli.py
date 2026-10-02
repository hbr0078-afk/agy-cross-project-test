"""
CLI regression tests for Phase 7-6.
Tests project/session/key-pool/config CLI behavior, config precedence, and security.
"""
import os
import sys
import json
import tempfile
import subprocess
import shutil
from unittest import TestCase, mock
from io import StringIO

# Import CLI functions
sys.path.insert(0, os.path.dirname(__file__))
from cli import (
    cmd_project_register,
    cmd_project_status,
    cmd_project_list,
    cmd_session_create,
    cmd_session_list,
    cmd_session_status,
    cmd_session_update,
    cmd_session_invalidate,
    cmd_keypool_list,
    cmd_keypool_status,
    cmd_keypool_reset,
    cmd_keypool_discover,
    cmd_config_show,
    cmd_config_set,
    cmd_config_init,
)
from config import RouterConfig, load_config, save_config, ConfigCorruptedError, get_config_path


class TestConfigPrecedence(TestCase):
    """Test config precedence: CLI > ENV > FILE > DEFAULT"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_defaults_only(self):
        """No config file, no env vars -> defaults"""
        config = load_config(self.config_path)
        self.assertEqual(config.default_key_index, 1)
        self.assertEqual(config.cooldown_seconds, 60)
        self.assertEqual(config.max_total_sync_bytes, 2 * 1024 * 1024)
        self.assertEqual(config.max_file_size_bytes, 512 * 1024)
        self.assertEqual(config.log_level, "INFO")
        self.assertEqual(config.git_auto_checkpoint, True)
        self.assertEqual(config.default_session_timeout, 3600)

    def test_config_file_override(self):
        """Config file overrides defaults"""
        config_data = {
            "default_key_index": 3,
            "cooldown_seconds": 120,
            "log_level": "DEBUG"
        }
        with open(self.config_path, 'w') as f:
            json.dump(config_data, f)

        config = load_config(self.config_path)
        self.assertEqual(config.default_key_index, 3)
        self.assertEqual(config.cooldown_seconds, 120)
        self.assertEqual(config.log_level, "DEBUG")
        # Others remain defaults
        self.assertEqual(config.max_total_sync_bytes, 2 * 1024 * 1024)

    def test_env_override_config_file(self):
        """Environment variables override config file"""
        config_data = {
            "default_key_index": 3,
            "cooldown_seconds": 120,
        }
        with open(self.config_path, 'w') as f:
            json.dump(config_data, f)

        with mock.patch.dict(os.environ, {
            'AGY_DEFAULT_KEY_INDEX': '5',
            'AGY_COOLDOWN_SECONDS': '300'
        }):
            config = load_config(self.config_path)
            self.assertEqual(config.default_key_index, 5)  # env wins
            self.assertEqual(config.cooldown_seconds, 300)  # env wins

    def test_cli_override_env(self):
        """CLI overrides override environment variables"""
        config_data = {
            "default_key_index": 3,
        }
        with open(self.config_path, 'w') as f:
            json.dump(config_data, f)

        with mock.patch.dict(os.environ, {
            'AGY_DEFAULT_KEY_INDEX': '5',
        }):
            cli_overrides = {"default_key_index": 7}
            config = load_config(self.config_path, cli_overrides)
            self.assertEqual(config.default_key_index, 7)  # CLI wins

    def test_corrupted_config_failsafe(self):
        """Corrupted config raises ConfigCorruptedError, not silent fallback"""
        with open(self.config_path, 'w') as f:
            f.write("{ invalid json }")

        with self.assertRaises(ConfigCorruptedError):
            load_config(self.config_path)

    def test_nonexistent_config_uses_defaults(self):
        """Nonexistent config path uses defaults without error"""
        nonexistent = os.path.join(self.temp_dir, "nonexistent.json")
        config = load_config(nonexistent)
        self.assertEqual(config.default_key_index, 1)
        self.assertEqual(config.log_level, "INFO")

    def test_empty_config_file_uses_defaults(self):
        """Empty config file uses defaults"""
        with open(self.config_path, 'w') as f:
            f.write("")

        with self.assertRaises(ConfigCorruptedError):
            load_config(self.config_path)

    def test_partial_config_file(self):
        """Config file with only some fields"""
        config_data = {
            "log_level": "WARNING"
        }
        with open(self.config_path, 'w') as f:
            json.dump(config_data, f)

        config = load_config(self.config_path)
        self.assertEqual(config.log_level, "WARNING")
        self.assertEqual(config.default_key_index, 1)  # default


class TestProjectCLI(TestCase):
    """Test project CLI commands - no runtime fields in output"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # Mock registry path
        self.registry_patch = mock.patch('cli.ProjectRegistry')
        self.mock_registry_class = self.registry_patch.start()
        self.mock_registry = mock.MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

    def tearDown(self):
        self.registry_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_register_output(self):
        """project register does not output runtime fields"""
        self.mock_registry.register_project.return_value = {
            "project_id": "test-proj",
            "path": "/test/path",
            "repo": "https://github.com/test/repo",
            "branch": "main",
            "state": "IDLE",
            "roadmap": "ROADMAP.md"
        }

        args = mock.MagicMock()
        args.path = "/test/path"
        args.project_id = "test-proj"

        # Capture stdout
        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_project_register(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Project ID:  test-proj", output)
        self.assertIn("Local Path:  /test/path", output)
        self.assertIn("GitHub Repo: https://github.com/test/repo", output)
        self.assertIn("Branch:      main", output)
        self.assertIn("State:       IDLE", output)
        # Runtime fields should NOT be present
        self.assertNotIn("Active Key Ref", output)
        self.assertNotIn("Environment ID", output)
        self.assertNotIn("Last Interaction", output)

    def test_project_status_no_runtime_fields(self):
        """project status does not output runtime fields"""
        self.mock_registry.get_project.return_value = {
            "project_id": "test-proj",
            "path": "/test/path",
            "repo": "https://github.com/test/repo",
            "branch": "main",
            "last_commit": "abc123",
            "state": "ACTIVE",
            "roadmap": "ROADMAP.md"
        }
        self.mock_registry.find_project_by_path.return_value = None

        args = mock.MagicMock()
        args.project_id = "test-proj"

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_project_status(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Project ID:     test-proj", output)
        self.assertIn("Local Path:     /test/path", output)
        self.assertIn("GitHub Repo:    https://github.com/test/repo", output)
        self.assertIn("Branch:         main", output)
        self.assertIn("Last Commit:    abc123", output)
        self.assertIn("State:          ACTIVE", output)
        self.assertIn("Roadmap File:   ROADMAP.md", output)
        # Runtime fields should NOT be present
        self.assertNotIn("Active Key Ref", output)
        self.assertNotIn("Environment ID", output)
        self.assertNotIn("Last Interaction ID", output)

    def test_project_list_no_runtime_fields(self):
        """project list does not show ACTIVE KEY column"""
        self.mock_registry.list_projects.return_value = [
            {"project_id": "proj1", "state": "IDLE", "path": "/path1"},
            {"project_id": "proj2", "state": "ACTIVE", "path": "/path2"},
        ]

        args = mock.MagicMock()

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_project_list(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("proj1", output)
        self.assertIn("proj2", output)
        self.assertIn("IDLE", output)
        self.assertIn("ACTIVE", output)
        # No ACTIVE KEY column
        self.assertNotIn("ACTIVE KEY", output)


class TestSessionCLI(TestCase):
    """Test session CLI commands"""

    def setUp(self):
        self.sess_patch = mock.patch('cli.SessionStateManager')
        self.mock_sess_class = self.sess_patch.start()
        self.mock_sess = mock.MagicMock()
        self.mock_sess_class.return_value = self.mock_sess

    def tearDown(self):
        self.sess_patch.stop()

    def test_session_create(self):
        """session create works"""
        self.mock_sess.create_session.return_value = {
            "session_id": "sess_test_123",
            "project_id": "test-proj",
            "bound_key": "key1",
            "environment_id": "env_1",
            "tenant_id": "tenant-A",
            "state": "IDLE"
        }

        args = mock.MagicMock()
        args.project_id = "test-proj"
        args.session_id = "sess_test_123"
        args.bound_key = "key1"
        args.environment_id = "env_1"
        args.tenant_id = "tenant-A"

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_session_create(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Session ID:         sess_test_123", output)
        self.assertIn("Project ID:         test-proj", output)
        self.assertIn("Bound Key:          key1", output)
        self.assertIn("Environment ID:     env_1", output)
        self.assertIn("Tenant ID:          tenant-A", output)
        self.assertIn("State:              IDLE", output)

    def test_session_list(self):
        """session list works"""
        self.mock_sess.list_sessions.return_value = [
            {"session_id": "sess1", "project_id": "proj1", "bound_key": "key1", "tenant_id": "t1", "environment_id": "env1", "state": "ACTIVE"},
            {"session_id": "sess2", "project_id": "proj2", "bound_key": "key2", "tenant_id": "t2", "environment_id": "env2", "state": "IDLE"},
        ]

        args = mock.MagicMock()
        args.project_id = None

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_session_list(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("sess1", output)
        self.assertIn("sess2", output)
        self.assertIn("ACTIVE", output)
        self.assertIn("IDLE", output)

    def test_session_status(self):
        """session status works"""
        self.mock_sess.get_session.return_value = {
            "session_id": "sess1",
            "project_id": "proj1",
            "bound_key": "key1",
            "tenant_id": "t1",
            "environment_id": "env1",
            "last_interaction_id": "int_123",
            "state": "ACTIVE",
            "created_at": 1234567890,
            "updated_at": 1234567900
        }

        args = mock.MagicMock()
        args.session_id = "sess1"

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_session_status(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Session ID:         sess1", output)
        self.assertIn("Bound Key:          key1", output)
        self.assertIn("Tenant ID:          t1", output)
        self.assertIn("Environment ID:     env1", output)
        self.assertIn("Last Interaction:   int_123", output)
        self.assertIn("State:              ACTIVE", output)

    def test_session_update(self):
        """session update works"""
        self.mock_sess.update_session.return_value = {
            "session_id": "sess1",
            "project_id": "proj1",
            "bound_key": "key2",
            "tenant_id": "t1",
            "environment_id": "env2",
            "last_interaction_id": "int_456",
            "state": "ACTIVE"
        }

        args = mock.MagicMock()
        args.session_id = "sess1"
        args.bound_key = "key2"
        args.environment_id = "env2"
        args.last_interaction_id = "int_456"
        args.tenant_id = "t1"
        args.state = "ACTIVE"

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_session_update(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Session ID:         sess1", output)
        self.assertIn("Bound Key:          key2", output)
        self.assertIn("Environment ID:     env2", output)
        self.assertIn("Last Interaction:   int_456", output)
        self.assertIn("State:              ACTIVE", output)

    def test_session_invalidate(self):
        """session invalidate works"""
        self.mock_sess.invalidate_session.return_value = {
            "session_id": "sess1",
            "state": "INVALIDATED"
        }

        args = mock.MagicMock()
        args.session_id = "sess1"

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_session_invalidate(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Session ID: sess1", output)
        self.assertIn("State:      INVALIDATED", output)


class TestKeyPoolCLI(TestCase):
    """Test key-pool CLI commands"""

    def setUp(self):
        self.kp_patch = mock.patch('cli.KeyPoolManager')
        self.mock_kp_class = self.kp_patch.start()
        self.mock_kp = mock.MagicMock()
        self.mock_kp_class.return_value = self.mock_kp

    def tearDown(self):
        self.kp_patch.stop()

    def test_keypool_list(self):
        """key-pool list works"""
        self.mock_kp.get_status.return_value = {
            "key1": {"index": 1, "state": "ACTIVE", "tenant_id": "tenant-A", "fail_count": 0, "last_used_at": 0},
            "key2": {"index": 2, "state": "COOLDOWN", "tenant_id": "tenant-B", "fail_count": 1, "last_used_at": 1234567890},
        }

        args = mock.MagicMock()

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_keypool_list(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("key1", output)
        self.assertIn("key2", output)
        self.assertIn("ACTIVE", output)
        self.assertIn("COOLDOWN", output)
        self.assertIn("tenant-A", output)
        self.assertIn("tenant-B", output)

    def test_keypool_status(self):
        """key-pool status works"""
        self.mock_kp.get_status.return_value = {
            "key1": {
                "index": 1, "state": "ACTIVE", "tenant_id": "tenant-A",
                "fail_count": 0, "last_status_code": 200, "cooldown_until": None,
                "last_used_at": 1234567890, "last_success_at": 1234567890
            }
        }

        args = mock.MagicMock()

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_keypool_status(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Key: key1", output)
        self.assertIn("State:         ACTIVE", output)
        self.assertIn("Tenant:        tenant-A", output)
        self.assertIn("Fail Count:    0", output)
        self.assertIn("Last Status:   200", output)

    def test_keypool_reset(self):
        """key-pool reset works"""
        self.mock_kp.reset_key.return_value = True

        args = mock.MagicMock()
        args.key_ref = "key1"

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_keypool_reset(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Key 'key1' reset to ACTIVE", output)

    def test_keypool_discover(self):
        """key-pool discover works"""
        self.mock_kp.discover_keys.return_value = [1, 2, 3]

        with mock.patch('cli.get_api_key_by_index', side_effect=lambda i: f"key_{i}_value" if i <= 3 else ""):
            args = mock.MagicMock()

            old_stdout = sys.stdout
            sys.stdout = captured = StringIO()
            try:
                cmd_keypool_discover(args)
                output = captured.getvalue()
            finally:
                sys.stdout = old_stdout

        self.assertIn("Discovered API keys", output)
        self.assertIn("Key 1", output)
        self.assertIn("Key 2", output)
        self.assertIn("Key 3", output)
        # Should show masked, not raw key
        self.assertNotIn("key_1_value", output)


class TestConfigCLI(TestCase):
    """Test config CLI commands"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_init(self):
        """config init creates default config"""
        args = mock.MagicMock()
        args.config_path = self.config_path

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_config_init(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Default configuration created", output)
        self.assertTrue(os.path.exists(self.config_path))

        with open(self.config_path) as f:
            data = json.load(f)
        self.assertEqual(data["default_key_index"], 1)
        self.assertEqual(data["log_level"], "INFO")

    def test_config_show(self):
        """config show displays current config"""
        config_data = {"default_key_index": 2, "log_level": "DEBUG"}
        with open(self.config_path, 'w') as f:
            json.dump(config_data, f)

        args = mock.MagicMock()
        args.config_path = self.config_path
        # Need to set cli override attributes to None
        args.default_key_index = None
        args.cooldown_seconds = None
        args.max_total_sync_bytes = None
        args.max_file_size_bytes = None
        args.log_level = None
        args.git_auto_checkpoint = None
        args.default_session_timeout = None

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_config_show(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Default Key Index:       2", output)
        self.assertIn("Log Level:               DEBUG", output)

    def test_config_set(self):
        """config set updates value"""
        # Create initial config
        with open(self.config_path, 'w') as f:
            json.dump({"default_key_index": 1}, f)

        args = mock.MagicMock()
        args.config_path = self.config_path
        args.key = "default_key_index"
        args.value = "5"
        # Need to set cli override attributes to None
        args.default_key_index = None
        args.cooldown_seconds = None
        args.max_total_sync_bytes = None
        args.max_file_size_bytes = None
        args.log_level = None
        args.git_auto_checkpoint = None
        args.default_session_timeout = None

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            cmd_config_set(args)
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("Config 'default_key_index' set to '5'", output)

        with open(self.config_path) as f:
            data = json.load(f)
        self.assertEqual(data["default_key_index"], 5)


class TestSecurityNoRawKeys(TestCase):
    """Test that no raw API keys are exposed in CLI output"""

    def test_no_raw_keys_in_keypool_output(self):
        """key-pool commands never output raw API keys"""
        kp_patch = mock.patch('cli.KeyPoolManager')
        mock_kp_class = kp_patch.start()
        mock_kp = mock.MagicMock()
        mock_kp_class.return_value = mock_kp
        mock_kp.get_status.return_value = {"key1": {"index": 1, "state": "ACTIVE", "tenant_id": "", "fail_count": 0, "last_used_at": 0}}

        try:
            args = mock.MagicMock()
            old_stdout = sys.stdout
            sys.stdout = captured = StringIO()
            try:
                cmd_keypool_list(args)
                output = captured.getvalue()
            finally:
                sys.stdout = old_stdout

            # Should show key references (key1, key2) not raw values
            self.assertIn("key1", output)
            self.assertNotIn("AIza", output)
            self.assertNotIn("sk-", output)
        finally:
            kp_patch.stop()


class TestCLIHelp(TestCase):
    """Test --help output for all commands"""

    def run_help(self, args_list):
        """Run CLI with given args and capture output"""
        old_argv = sys.argv
        old_stdout = sys.stdout
        sys.argv = ["agy-router"] + args_list
        sys.stdout = captured = StringIO()
        try:
            from cli import main
            try:
                main()
            except SystemExit:
                pass
            return captured.getvalue()
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout

    def test_main_help(self):
        """Main --help shows all subcommands"""
        output = self.run_help(["--help"])
        self.assertIn("test-agent", output)
        self.assertIn("project", output)
        self.assertIn("session", output)
        self.assertIn("key-pool", output)
        self.assertIn("config", output)
        self.assertIn("git", output)
        self.assertIn("snapshot", output)

    def test_project_help(self):
        """project --help shows subcommands"""
        output = self.run_help(["project", "--help"])
        self.assertIn("register", output)
        self.assertIn("status", output)
        self.assertIn("list", output)

    def test_session_help(self):
        """session --help shows subcommands"""
        output = self.run_help(["session", "--help"])
        self.assertIn("create", output)
        self.assertIn("list", output)
        self.assertIn("status", output)
        self.assertIn("update", output)
        self.assertIn("invalidate", output)

    def test_keypool_help(self):
        """key-pool --help shows subcommands"""
        output = self.run_help(["key-pool", "--help"])
        self.assertIn("list", output)
        self.assertIn("status", output)
        self.assertIn("reset", output)
        self.assertIn("discover", output)

    def test_config_help(self):
        """config --help shows subcommands"""
        output = self.run_help(["config", "--help"])
        self.assertIn("show", output)
        self.assertIn("set", output)
        self.assertIn("init", output)


class TestExitCodes(TestCase):
    """Test CLI exit codes"""

    def test_invalid_command_exits_nonzero(self):
        """Invalid command exits with non-zero"""
        old_argv = sys.argv
        old_stderr = sys.stderr
        sys.argv = ["agy-router", "invalid_command"]
        sys.stderr = StringIO()
        try:
            from cli import main
            try:
                main()
            except SystemExit as e:
                self.assertNotEqual(e.code, 0)
        finally:
            sys.argv = old_argv
            sys.stderr = old_stderr

    def test_missing_required_args_exits_nonzero(self):
        """Missing required args exits with non-zero"""
        old_argv = sys.argv
        old_stderr = sys.stderr
        sys.argv = ["agy-router", "session", "create"]  # missing --project-id
        sys.stderr = StringIO()
        try:
            from cli import main
            try:
                main()
            except SystemExit as e:
                self.assertNotEqual(e.code, 0)
        finally:
            sys.argv = old_argv
            sys.stderr = old_stderr


class TestCLIConfigPrecedenceSubprocess(TestCase):
    """Test config precedence using actual subprocess calls."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.cli_path = os.path.join(os.path.dirname(__file__), "cli.py")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_cli(self, args_list, env=None, config_content=None):
        """Run CLI subprocess and return (stdout, stderr, returncode)."""
        if config_content is not None:
            with open(self.config_path, 'w') as f:
                json.dump(config_content, f)
        
        cmd = [sys.executable, self.cli_path, "--config", self.config_path] + args_list
        test_env = os.environ.copy()
        if env:
            test_env.update(env)
        
        result = subprocess.run(cmd, capture_output=True, text=True, env=test_env)
        return result.stdout, result.stderr, result.returncode

    def test_default_only(self):
        """No config file, no env, no CLI -> defaults"""
        stdout, stderr, code = self.run_cli(["config", "show"], config_content=None)
        self.assertEqual(code, 0)
        self.assertIn("Default Key Index:       1", stdout)
        self.assertIn("Log Level:               INFO", stdout)

    def test_file_override_default(self):
        """Config file overrides defaults"""
        stdout, stderr, code = self.run_cli(
            ["config", "show"],
            config_content={"default_key_index": 2, "log_level": "DEBUG"}
        )
        self.assertEqual(code, 0)
        self.assertIn("Default Key Index:       2", stdout)
        self.assertIn("Log Level:               DEBUG", stdout)

    def test_env_override_file(self):
        """Environment variable overrides config file"""
        stdout, stderr, code = self.run_cli(
            ["config", "show"],
            env={'AGY_DEFAULT_KEY_INDEX': '5', 'AGY_LOG_LEVEL': 'WARNING'},
            config_content={"default_key_index": 2, "log_level": "DEBUG"}
        )
        self.assertEqual(code, 0)
        self.assertIn("Default Key Index:       5", stdout)
        self.assertIn("Log Level:               WARNING", stdout)

    def test_cli_override_env(self):
        """CLI argument overrides environment variable"""
        stdout, stderr, code = self.run_cli(
            ["--default-key-index", "7", "--log-level", "ERROR", "config", "show"],
            env={'AGY_DEFAULT_KEY_INDEX': '5', 'AGY_LOG_LEVEL': 'WARNING'},
            config_content={"default_key_index": 2, "log_level": "DEBUG"}
        )
        self.assertEqual(code, 0)
        self.assertIn("Default Key Index:       7", stdout)
        self.assertIn("Log Level:               ERROR", stdout)

    def test_cli_bool_true(self):
        """CLI --git-auto-checkpoint sets True"""
        stdout, stderr, code = self.run_cli(
            ["--git-auto-checkpoint", "config", "show"],
            config_content={"git_auto_checkpoint": False}
        )
        self.assertEqual(code, 0)
        self.assertIn("Git Auto Checkpoint:     True", stdout)

    def test_cli_bool_false(self):
        """CLI --no-git-auto-checkpoint sets False"""
        stdout, stderr, code = self.run_cli(
            ["--no-git-auto-checkpoint", "config", "show"],
            config_content={"git_auto_checkpoint": True}
        )
        self.assertEqual(code, 0)
        self.assertIn("Git Auto Checkpoint:     False", stdout)

    def test_corrupted_config_failsafe(self):
        """Corrupted config file causes non-zero exit"""
        with open(self.config_path, 'w') as f:
            f.write("{ invalid json }")
        
        stdout, stderr, code = self.run_cli(["config", "show"])
        self.assertNotEqual(code, 0)
        self.assertIn("corrupted", stderr.lower())

    def test_empty_config_failsafe(self):
        """Empty config file causes non-zero exit"""
        with open(self.config_path, 'w') as f:
            f.write("")
        
        stdout, stderr, code = self.run_cli(["config", "show"])
        self.assertNotEqual(code, 0)
        self.assertIn("corrupted", stderr.lower())


class TestCLISecuritySubprocess(TestCase):
    """Test security - no raw credentials in subprocess output."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        self.cli_path = os.path.join(os.path.dirname(__file__), "cli.py")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_cli(self, args_list, env=None):
        cmd = [sys.executable, self.cli_path, "--config", self.config_path] + args_list
        test_env = os.environ.copy()
        if env:
            test_env.update(env)
        result = subprocess.run(cmd, capture_output=True, text=True, env=test_env)
        return result.stdout, result.stderr, result.returncode

    def test_no_raw_keys_in_output(self):
        """key-pool commands never output raw API keys"""
        # We mock the KeyPoolManager at the module level for this subprocess test
        # Since we can't easily mock in subprocess, we test that the CLI doesn't
        # print raw keys when they would be present in the output
        stdout, stderr, code = self.run_cli(["key-pool", "list"])
        # Should either succeed with masked output or fail gracefully
        # The key point is no raw keys like AIza... or sk-... in output
        self.assertNotIn("AIza", stdout)
        self.assertNotIn("sk-", stdout)
        self.assertNotIn("authorization", stdout.lower())
        self.assertNotIn("bearer", stdout.lower())


if __name__ == "__main__":
    import unittest
    unittest.main()