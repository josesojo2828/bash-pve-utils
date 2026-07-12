#!/usr/bin/env python3
"""
bash-pve-utils: Local Python CLI and Remote Orchestrator
Uses native SSH multiplexing (ControlMaster) to run commands efficiently.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

# Optional YAML support
try:
    import yaml as _yaml

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

SOCKET_PATH = "/tmp/bash-pve-utils-ssh-socket"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# ── Logging ──────────────────────────────────────────────────────────────

logger = logging.getLogger("forge")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


# ── Helpers ──────────────────────────────────────────────────────────────


def _run_local(
    cmd: list[str],
    capture: bool = True,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a local command and return the result."""
    logger.debug("local: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        logger.error("Local command failed: %s", " ".join(cmd))
        if capture and e.stderr:
            logger.error("stderr: %s", e.stderr.strip())
        raise
    except subprocess.TimeoutExpired as e:
        logger.error("Local command timed out: %s", " ".join(cmd))
        raise


def _to_b64(val: str) -> str:
    """Encode a string to base64. An empty string encodes to empty string."""
    if not val:
        return ""
    return base64.b64encode(val.encode("utf-8")).decode("utf-8")


# ── Orchestrator ─────────────────────────────────────────────────────────


class PVEOrchestrator:
    """Manages a multiplexed SSH connection to a Proxmox VE host."""

    def __init__(self, host: str, user: str, port: int) -> None:
        self.host = host
        self.user = user
        self.port = str(port)
        self.socket = f"{SOCKET_PATH}-{self.user}@{self.host}:{self.port}"
        self._connected = False

    # ── Context manager ──────────────────────────────────────────────

    def __enter__(self) -> PVEOrchestrator:
        self.establish_connection()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close_connection()

    # ── Connection lifecycle ─────────────────────────────────────────

    def establish_connection(self) -> None:
        """Start the master SSH connection in the background.

        Uses BatchMode=yes so it fails fast if there are no SSH keys configured,
        rather than hanging on a password prompt.
        """
        if self._connected:
            logger.debug("SSH connection already established, reusing")
            return

        logger.info(
            "Establishing SSH connection to %s@%s:%s",
            self.user,
            self.host,
            self.port,
        )

        # Clean stale socket
        self._clean_socket()

        cmd = [
            "ssh",
            "-M",
            "-S", self.socket,
            "-f", "-N",
            "-p", self.port,
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-o", "ControlPersist=60",
            f"{self.user}@{self.host}",
        ]

        try:
            _run_local(cmd)
            self._connected = True
            logger.info("SSH multiplexed connection established")
        except subprocess.CalledProcessError:
            logger.error(
                "Failed to establish SSH connection to %s@%s:%s. "
                "Verify host, port, and SSH keys are configured.",
                self.user,
                self.host,
                self.port,
            )
            sys.exit(1)

    def close_connection(self) -> None:
        """Close the master connection and clean up the socket."""
        if not self._connected and not os.path.exists(self.socket):
            return

        logger.info("Closing SSH connection...")
        cmd = [
            "ssh",
            "-S", self.socket,
            "-O", "exit",
            "-p", self.port,
            f"{self.user}@{self.host}",
        ]
        subprocess.run(cmd, capture_output=True, timeout=10)
        self._clean_socket()
        self._connected = False
        logger.info("SSH connection closed")

    def _clean_socket(self) -> None:
        if os.path.exists(self.socket):
            try:
                os.remove(self.socket)
            except OSError as e:
                logger.warning("Failed to remove stale socket: %s", e)

    # ── Remote execution ─────────────────────────────────────────────

    def run_remote(
        self,
        cmd_str: str,
        capture: bool = True,
        stream: bool = False,
    ) -> subprocess.CompletedProcess | None:
        """Run a command remotely reusing the multiplexed SSH connection."""
        ssh_cmd = [
            "ssh",
            "-S", self.socket,
            "-p", self.port,
            f"{self.user}@{self.host}",
            cmd_str,
        ]

        logger.debug("remote: %s", cmd_str)

        if stream:
            return self._run_remote_stream(ssh_cmd)

        try:
            return subprocess.run(
                ssh_cmd,
                capture_output=capture,
                text=True,
                check=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as e:
            logger.error("Remote command failed: %s", cmd_str)
            if capture and e.stderr:
                logger.error("stderr: %s", e.stderr.strip())
            raise
        except subprocess.TimeoutExpired:
            logger.error("Remote command timed out: %s", cmd_str)
            raise

    def _run_remote_stream(
        self, ssh_cmd: list[str]
    ) -> subprocess.CompletedProcess | None:
        """Run a remote command and stream its output in real time."""
        process = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        while True:
            output = process.stdout.readline()
            if output == "" and process.poll() is not None:
                break
            if output:
                print(output.strip())

        rc = process.poll()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, ssh_cmd)
        return None

    # ── File transfer ────────────────────────────────────────────────

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a file via SCP using the multiplexed SSH socket."""
        scp_cmd = [
            "scp",
            "-o", f"ControlPath={self.socket}",
            "-P", self.port,
            local_path,
            f"{self.user}@{self.host}:{remote_path}",
        ]
        logger.info("Uploading %s -> %s:%s", local_path, self.host, remote_path)
        try:
            subprocess.run(scp_cmd, capture_output=True, check=True, timeout=60)
        except subprocess.CalledProcessError as e:
            logger.error("Failed to upload file %s", local_path)
            if e.stderr:
                logger.error("scp stderr: %s", e.stderr.strip())
            raise


# ── Commands ─────────────────────────────────────────────────────────────


def _load_config(config_path: str) -> dict:
    """Load and validate a config file (JSON or YAML)."""
    if not os.path.exists(config_path):
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    ext = os.path.splitext(config_path)[1].lower()

    try:
        with open(config_path) as f:
            if ext in (".yml", ".yaml"):
                if not _HAS_YAML:
                    logger.error(
                        "YAML config requires pyyaml. Install with: pip install pyyaml"
                    )
                    sys.exit(1)
                cfg = _yaml.safe_load(f)
            else:
                cfg = json.load(f)
    except (json.JSONDecodeError, _yaml.YAMLError) as e:
        logger.error("Failed to parse config: %s", e)
        sys.exit(1)

    if not isinstance(cfg, dict):
        logger.error("Config must be a JSON/YAML object")
        sys.exit(1)

    if "containers" not in cfg:
        logger.warning("Config has no 'containers' key — nothing to do")
        return cfg

    return cfg


def cmd_apply(
    orch: PVEOrchestrator, config_path: str, dry_run: bool
) -> None:
    """Apply declarative configuration state to remote Proxmox node."""
    config = _load_config(config_path)
    storage = config.get("storage", "local-lvm")
    template_storage = config.get("template_storage", "local")
    containers = config.get("containers", [])

    if not containers:
        logger.info("No containers defined in config")
        return

    with orch:
        if dry_run:
            _dry_run_check(orch, containers)
            return

        _execute_provision(orch, containers, storage, template_storage)

    logger.info("All provisioning tasks processed")


def _dry_run_check(orch: PVEOrchestrator, containers: list[dict]) -> None:
    """Read-only check of what would change."""
    logger.info("── Dry-run mode (read-only) ──")
    for ct in containers:
        vmid = str(ct.get("vmid"))
        hostname = ct.get("hostname", f"lxc-{vmid}")
        logger.info("Checking VMID %s (%s)...", vmid, hostname)

        res = orch.run_remote(f"pct status {vmid} 2>/dev/null || echo 'not_found'")
        status = res.stdout.strip()

        if "not_found" in status:
            logger.warning("  [Action] Container %s will be CREATED", vmid)
        else:
            logger.info("  Container exists (%s). Checking config diff:", status)
            conf_res = orch.run_remote(f"pct config {vmid}")
            conf_lines = conf_res.stdout.splitlines()

            for spec_key, config_key in [
                ("cores", "cores"),
                ("memory", "memory"),
                ("swap", "swap"),
                ("onboot", "onboot"),
            ]:
                desired = ct.get(spec_key)
                if desired is not None:
                    current = ""
                    for line in conf_lines:
                        if line.startswith(f"{config_key}:"):
                            _, val = line.split(":", 1)
                            current = val.strip()
                            break
                    if str(current) != str(desired):
                        logger.warning(
                            "  [Diff] %s: current=%s → desired=%s",
                            spec_key,
                            current,
                            desired,
                        )


def _execute_provision(
    orch: PVEOrchestrator,
    containers: list[dict],
    storage: str,
    template_storage: str,
) -> None:
    """Upload and execute the provisioning script for each container."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_script = os.path.join(script_dir, "scripts", "provision.sh")
    remote_script = "/tmp/pve_provision.sh"

    logger.info("Uploading provision script to Proxmox server...")
    orch.upload_file(local_script, remote_script)
    orch.run_remote(f"chmod +x {remote_script}")

    for ct in containers:
        vmid = str(ct.get("vmid"))
        hostname = ct.get("hostname", f"lxc-{vmid}")
        ostemplate = ct.get("ostemplate")

        if not ostemplate:
            logger.error("Missing 'ostemplate' for VMID %s — skipping", vmid)
            continue

        args = {
            "vmid": vmid,
            "hostname": hostname,
            "cores": str(ct.get("cores", 1)),
            "memory": str(ct.get("memory", 512)),
            "swap": str(ct.get("swap", 512)),
            "disk": ct.get("disk", "8G"),
            "ostemplate": ostemplate,
            "bridge": ct.get("bridge", "vmbr0"),
            "ip": ct.get("ip", "dhcp"),
            "gateway": ct.get("gateway", ""),
            "ssh_key_b64": _to_b64(ct.get("ssh_key", "")),
            "onboot": str(ct.get("onboot", 0)),
            "storage": storage,
            "template_storage": template_storage,
            "bootstrap_b64": _to_b64(ct.get("bootstrap", "")),
        }

        logger.info("Provisioning VMID %s (%s)...", vmid, hostname)

        run_cmd = (
            f"{remote_script}"
            f" '{args['vmid']}' '{args['hostname']}' '{args['cores']}'"
            f" '{args['memory']}' '{args['swap']}' '{args['disk']}'"
            f" '{args['ostemplate']}' '{args['bridge']}' '{args['ip']}'"
            f" '{args['gateway']}' '{args['ssh_key_b64']}' '{args['onboot']}'"
            f" '{args['storage']}' '{args['template_storage']}'"
            f" '{args['bootstrap_b64']}'"
        )

        try:
            orch.run_remote(run_cmd, stream=True)
        except subprocess.CalledProcessError:
            logger.error("Failed to provision container %s", vmid)

    # Clean up
    orch.run_remote(f"rm -f {remote_script}")


def cmd_status(orch: PVEOrchestrator, config_path: str) -> None:
    """Show status of containers defined in local config."""
    config = _load_config(config_path)
    containers = config.get("containers", [])

    if not containers:
        logger.info("No containers defined in config")
        return

    with orch:
        logger.info("Fetching remote LXC container list...")
        res = orch.run_remote("pct list")
        lines = res.stdout.splitlines()

        active: dict[str, dict] = {}
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 3:
                active[parts[0]] = {"status": parts[1], "name": parts[2]}

        print()
        print(f"{'VMID':^10} | {'Hostname':^20} | {'Status':^10} | {'IP Config':^20}")
        print("-" * 70)
        for ct in containers:
            vmid = str(ct.get("vmid"))
            hostname = ct.get("hostname", "")
            ip = ct.get("ip", "")

            if vmid in active:
                st = active[vmid]["status"]
                if st == "running":
                    status_display = "\033[92mrunning\033[0m"
                else:
                    status_display = "\033[93mstopped\033[0m"
            else:
                status_display = "\033[90mnot found\033[0m"

            print(
                f"{vmid:^10} | {hostname:<20} | {status_display:^19} | {ip:<20}"
            )
        print()


def cmd_destroy(orch: PVEOrchestrator, vmid: int, force: bool = False) -> None:
    """Destroy a remote container by VMID."""
    if not force:
        confirm = input(
            f"\033[91m[Warning]\033[0m Destroy container {vmid}? [y/N]: "
        )
        if confirm.lower() != "y":
            logger.info("Destruction canceled")
            return

    with orch:
        logger.info("Stopping container %s (if running)...", vmid)
        orch.run_remote(f"pct stop {vmid} 2>/dev/null || true")
        logger.info("Destroying container %s...", vmid)
        orch.run_remote(f"pct destroy {vmid}")
        logger.info("Container %s destroyed successfully", vmid)


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="bash-pve-utils: Proxmox LXC Declarative Provisioner"
    )
    parser.add_argument(
        "host", help="Proxmox VE server hostname or IP address"
    )
    parser.add_argument(
        "--user", default="root", help="SSH user name (default: root)"
    )
    parser.add_argument(
        "--port", type=int, default=22, help="SSH port (default: 22)"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to config file (JSON or YAML, default: config.json)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # apply
    p_apply = subparsers.add_parser(
        "apply", help="Apply declarative configuration to remote Proxmox node"
    )
    p_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without modifying anything",
    )

    # status
    subparsers.add_parser(
        "status", help="Show status of containers defined in local config"
    )

    # destroy
    p_destroy = subparsers.add_parser(
        "destroy", help="Destroy a remote container"
    )
    p_destroy.add_argument(
        "vmid", type=int, help="VMID of the container to destroy"
    )
    p_destroy.add_argument(
        "-f", "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Resolve config path
    config_path = args.config
    if not os.path.isabs(config_path) and not os.path.exists(config_path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        alt = os.path.join(script_dir, config_path)
        if os.path.exists(alt):
            config_path = alt

    orch = PVEOrchestrator(args.host, args.user, args.port)

    try:
        if args.command == "apply":
            cmd_apply(orch, config_path, args.dry_run)
        elif args.command == "status":
            cmd_status(orch, config_path)
        elif args.command == "destroy":
            cmd_destroy(orch, args.vmid, args.force)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        orch.close_connection()
        sys.exit(130)


if __name__ == "__main__":
    main()
