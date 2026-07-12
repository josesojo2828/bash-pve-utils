"""Tests for forge.py — all SSH calls are mocked, zero external dependencies."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Ensure the parent directory is on sys.path so we can import forge
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forge  # noqa: E402


# ── _to_b64 ──────────────────────────────────────────────────────────────


class TestToB64(unittest.TestCase):
    def test_encodes_string(self):
        self.assertEqual(forge._to_b64("hello"), "aGVsbG8=")

    def test_encodes_empty_string(self):
        self.assertEqual(forge._to_b64(""), "")

    def test_encodes_multiline(self):
        result = forge._to_b64("line1\nline2")
        self.assertEqual(result, "bGluZTEKbGluZTI=")


# ── _load_config ─────────────────────────────────────────────────────────


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_loads_valid_json(self):
        cfg = {
            "storage": "local-lvm",
            "containers": [{"vmid": 900, "ostemplate": "debian-12.tar.zst"}],
        }
        path = self._write("config.json", json.dumps(cfg))
        result = forge._load_config(path)
        self.assertEqual(result, cfg)

    def test_exits_on_missing_file(self):
        with self.assertRaises(SystemExit):
            forge._load_config("/nonexistent/path.json")

    def test_exits_on_invalid_json(self):
        path = self._write("bad.json", "{invalid json}")
        with self.assertRaises(SystemExit):
            forge._load_config(path)

    @patch("forge._HAS_YAML", True)
    def test_loads_yaml_config(self):
        """Should load YAML config when pyyaml is available."""
        import types

        # Mock the yaml module
        mock_yaml = types.ModuleType("yaml")

        def safe_load(stream):
            import json as _json
            return _json.load(stream)

        mock_yaml.safe_load = safe_load
        mock_yaml.YAMLError = Exception

        with patch.dict("sys.modules", {"yaml": mock_yaml}), patch(
            "forge._yaml", mock_yaml
        ):
            cfg = {
                "storage": "local-lvm",
                "containers": [{"vmid": 900, "ostemplate": "deb-12.tar.zst"}],
            }
            path = self._write("config.yml", json.dumps(cfg))
            result = forge._load_config(path)
            self.assertEqual(result, cfg)

    @patch("forge._HAS_YAML", False)
    def test_exits_on_yaml_without_pyyaml(self):
        """Should exit with error when YAML file but pyyaml not installed."""
        path = self._write("config.yml", "storage: local-lvm\n")
        with self.assertRaises(SystemExit):
            forge._load_config(path)


# ── PVEOrchestrator ──────────────────────────────────────────────────────


class TestPVEOrchestrator(unittest.TestCase):
    def setUp(self):
        self.orch = forge.PVEOrchestrator("10.0.0.1", "root", 22)

    @patch("forge.subprocess.run")
    def test_establishes_connection(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        self.orch.establish_connection()

        cmd = mock_run.call_args[0][0]
        self.assertIn("ssh", cmd)
        self.assertIn("-M", cmd)  # ControlMaster
        self.assertIn("BatchMode=yes", str(cmd))  # fail fast
        self.assertTrue(self.orch._connected)

    @patch("forge.subprocess.run")
    def test_exits_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(255, ["ssh"])
        with self.assertRaises(SystemExit):
            self.orch.establish_connection()

    @patch("forge.subprocess.run")
    def test_context_manager(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with self.orch as o:
            self.assertIs(o, self.orch)
            self.assertTrue(self.orch._connected)
        self.assertFalse(self.orch._connected)

    @patch("forge.subprocess.run")
    def test_reuses_connection(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        self.orch.establish_connection()
        self.assertEqual(mock_run.call_count, 1)

        self.orch.establish_connection()
        # Should NOT call subprocess again
        self.assertEqual(mock_run.call_count, 1)

    @patch("forge.os.remove")
    @patch("forge.os.path.exists", return_value=True)
    @patch("forge.subprocess.run")
    def test_cleans_stale_socket(
        self, mock_run, mock_exists, mock_remove
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        self.orch.establish_connection()
        mock_remove.assert_called_once_with(self.orch.socket)

    @patch("forge.subprocess.run")
    def test_run_remote(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="running\n", stderr=""
        )
        self.orch._connected = True  # skip establish

        result = self.orch.run_remote("pct list")

        cmd = mock_run.call_args[0][0]
        self.assertIn("-S", cmd)
        self.assertIn(self.orch.socket, cmd)
        self.assertIn("pct list", cmd)
        self.assertEqual(result.stdout.strip(), "running")

    @patch("forge.subprocess.run")
    def test_upload_file(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        self.orch._connected = True

        self.orch.upload_file("/local/script.sh", "/tmp/script.sh")

        cmd = mock_run.call_args[0][0]
        self.assertIn("scp", cmd)
        self.assertIn("ControlPath", str(cmd))


# ── cmd_apply: dry-run ───────────────────────────────────────────────────


class TestCmdApplyDryRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orch = forge.PVEOrchestrator("10.0.0.1", "root", 22)
        self.cfg = {
            "storage": "local-lvm",
            "containers": [
                {
                    "vmid": 900,
                    "hostname": "test-ct",
                    "cores": 2,
                    "memory": 1024,
                    "ostemplate": "debian-12.tar.zst",
                    "ip": "dhcp",
                    "bridge": "vmbr0",
                }
            ],
        }
        self.config_path = os.path.join(self.tmpdir, "config.json")
        with open(self.config_path, "w") as f:
            json.dump(self.cfg, f)

    @patch("forge.subprocess.run")
    def test_dry_run_only_reads(self, mock_run):
        """Dry-run should only call pct status / pct config, never pct create."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="status: running\n", stderr=""
        )

        forge.cmd_apply(self.orch, self.config_path, dry_run=True)

        # Collect all remote command strings
        remote_cmds = []
        for c in mock_run.call_args_list:
            args = c[0][0]
            if "pct" in str(args):
                remote_cmds.append(" ".join(args))

        self.assertTrue(
            any("pct status" in cmd for cmd in remote_cmds),
            "dry-run should check status",
        )

        # Must NOT include destructive commands
        for cmd in remote_cmds:
            self.assertNotIn("pct create", cmd)
            self.assertNotIn("pct set", cmd)
            self.assertNotIn("pct destroy", cmd)

    @patch("forge.subprocess.run")
    def test_dry_run_new_container(self, mock_run):
        """When container doesn't exist, dry-run should handle 'not_found' gracefully."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not_found\n", stderr=""
        )

        forge.cmd_apply(self.orch, self.config_path, dry_run=True)

    @patch("forge.subprocess.run")
    def test_dry_run_config_diff(self, mock_run):
        """When config differs, should process diff output without error."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="status: running\n", stderr=""
                )
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="arch: amd64\ncores: 1\nmemory: 512\n",
                stderr="",
            )

        mock_run.side_effect = side_effect
        forge.cmd_apply(self.orch, self.config_path, dry_run=True)


# ── cmd_apply: full ──────────────────────────────────────────────────────


class TestCmdApply(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orch = forge.PVEOrchestrator("10.0.0.1", "root", 22)
        self.cfg = {
            "storage": "local-lvm",
            "containers": [
                {
                    "vmid": 900,
                    "hostname": "test-ct",
                    "cores": 2,
                    "memory": 1024,
                    "ostemplate": "debian-12.tar.zst",
                    "bridge": "vmbr0",
                    "ip": "dhcp",
                    "ssh_key": "ssh-ed25519 AAAA...",
                    "onboot": 1,
                    "bootstrap": "apt-get update",
                }
            ],
        }
        self.config_path = os.path.join(self.tmpdir, "config.json")
        with open(self.config_path, "w") as f:
            json.dump(self.cfg, f)

    @patch("forge.subprocess.Popen")
    @patch("forge.os.path.dirname")
    @patch("forge.os.path.exists", return_value=True)
    @patch("forge.subprocess.run")
    def test_apply_uploads_and_provisions(
        self, mock_run, mock_exists, mock_dirname, mock_popen
    ):
        mock_dirname.return_value = os.path.dirname(forge.__file__)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        # Mock Popen for streaming output (infinite read cycle)
        mock_proc = MagicMock()

        def readline_cycle():
            yield "line1\n"
            yield "line2\n"
            while True:
                yield ""

        mock_proc.stdout.readline.side_effect = readline_cycle()
        # Once readline returns "", poll returns 0 to break the loop
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        # Should not raise
        forge.cmd_apply(self.orch, self.config_path, dry_run=False)

        # Should have called chmod (provision script was uploaded)
        chmod_calls = [
            c
            for c in mock_run.call_args_list
            if "chmod" in " ".join(c[0][0])
        ]
        self.assertGreater(len(chmod_calls), 0)


# ── cmd_status ───────────────────────────────────────────────────────────


class TestCmdStatus(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orch = forge.PVEOrchestrator("10.0.0.1", "root", 22)
        self.cfg = {
            "containers": [
                {"vmid": 900, "hostname": "test-ct", "ip": "dhcp"},
            ],
        }
        self.config_path = os.path.join(self.tmpdir, "config.json")
        with open(self.config_path, "w") as f:
            json.dump(self.cfg, f)

    @patch("forge.subprocess.run")
    def test_status_parses_pct_list(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "VMID   Status     Name\n"
                "900    running    test-ct\n"
                "902    stopped    other-ct\n"
            ),
            stderr="",
        )

        forge.cmd_status(self.orch, self.config_path)


# ── cmd_destroy ──────────────────────────────────────────────────────────


class TestCmdDestroy(unittest.TestCase):
    def setUp(self):
        self.orch = forge.PVEOrchestrator("10.0.0.1", "root", 22)

    @patch("builtins.input", return_value="y")
    @patch("forge.subprocess.run")
    def test_destroy_with_confirmation(self, mock_run, mock_input):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        forge.cmd_destroy(self.orch, vmid=900)

        remote_cmds = []
        for c in mock_run.call_args_list:
            args = c[0][0]
            if "pct" in str(args):
                remote_cmds.append(" ".join(args))

        self.assertTrue(any("pct stop" in cmd for cmd in remote_cmds))
        self.assertTrue(any("pct destroy" in cmd for cmd in remote_cmds))

    @patch("builtins.input", return_value="n")
    @patch("forge.subprocess.run")
    def test_destroy_canceled(self, mock_run, mock_input):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        forge.cmd_destroy(self.orch, vmid=900)

        remote_cmds = []
        for c in mock_run.call_args_list:
            args = c[0][0]
            if "pct" in str(args):
                remote_cmds.append(" ".join(args))

        self.assertEqual(len(remote_cmds), 0)

    @patch("forge.subprocess.run")
    def test_destroy_force(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        forge.cmd_destroy(self.orch, vmid=900, force=True)

        remote_cmds = []
        for c in mock_run.call_args_list:
            args = c[0][0]
            if "pct" in str(args):
                remote_cmds.append(" ".join(args))

        self.assertTrue(any("pct stop" in cmd for cmd in remote_cmds))
        self.assertTrue(any("pct destroy" in cmd for cmd in remote_cmds))


if __name__ == "__main__":
    unittest.main()
