#!/usr/bin/env python3
"""
bash-pve-utils — PVE Server Inspector
Connects to a Proxmox VE host and displays server resources + container/VM info.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any

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
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    logger.debug("local: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check,
            timeout=timeout,
            env=env,
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
    if not val:
        return ""
    return base64.b64encode(val.encode("utf-8")).decode("utf-8")


# ── SSH helpers ──────────────────────────────────────────────────────────


def _check_sshpass() -> bool:
    return shutil.which("sshpass") is not None


def _prompt_credentials() -> dict:
    print()
    print("\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
    print("\u2551  Proxmox VE Connection              \u2551")
    print("\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
    host = input("IP Address: ").strip()
    if not host:
        logger.error("IP Address is required")
        sys.exit(1)
    user = input("SSH User [root]: ").strip() or "root"
    port_str = input("SSH Port [22]: ").strip() or "22"
    try:
        port = int(port_str)
    except ValueError:
        logger.error("Port must be a number")
        sys.exit(1)
    password = getpass.getpass("SSH Password (leave empty for key auth): ")
    return {"host": host, "user": user, "port": port, "password": password}


# ── Orchestrator ─────────────────────────────────────────────────────────


class PVEOrchestrator:
    """Manages a multiplexed SSH connection to a Proxmox VE host."""

    def __init__(self, host: str, user: str, port: int, password: str = "") -> None:
        self.host = host
        self.user = user
        self.port = str(port)
        self.password = password
        self.socket = f"{SOCKET_PATH}-{self.user}@{self.host}:{self.port}"
        self._connected = False

    def __enter__(self) -> PVEOrchestrator:
        self.establish_connection()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close_connection()

    def establish_connection(self) -> None:
        if self._connected:
            logger.debug("SSH connection already established, reusing")
            return

        logger.info("Establishing SSH connection to %s@%s:%s", self.user, self.host, self.port)
        self._clean_socket()

        ssh_opts = [
            "-M", "-S", self.socket, "-f", "-N",
            "-p", self.port,
            "-o", "ConnectTimeout=10",
            "-o", "ControlPersist=60",
        ]
        if not self.password:
            ssh_opts.extend(["-o", "BatchMode=yes"])

        cmd = ["ssh"] + ssh_opts + [f"{self.user}@{self.host}"]
        env = None

        if self.password:
            if not _check_sshpass():
                logger.error("Password auth requires sshpass. Install with: sudo apt install sshpass")
                sys.exit(1)
            cmd = ["sshpass", "-e"] + cmd
            env = os.environ.copy()
            env["SSHPASS"] = self.password

        try:
            _run_local(cmd, env=env)
            self._connected = True
            logger.info("SSH multiplexed connection established")
        except subprocess.CalledProcessError:
            logger.error("Failed to establish SSH connection to %s@%s:%s.", self.user, self.host, self.port)
            sys.exit(1)

    def close_connection(self) -> None:
        if not self._connected and not os.path.exists(self.socket):
            return
        logger.info("Closing SSH connection...")
        cmd = ["ssh", "-S", self.socket, "-O", "exit", "-p", self.port, f"{self.user}@{self.host}"]
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

    def run_remote(
        self, cmd_str: str, capture: bool = True, stream: bool = False, quiet: bool = False,
    ) -> subprocess.CompletedProcess | None:
        ssh_cmd = ["ssh", "-S", self.socket, "-p", self.port, f"{self.user}@{self.host}", cmd_str]
        logger.debug("remote: %s", cmd_str)

        if stream:
            process = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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

        try:
            return subprocess.run(ssh_cmd, capture_output=capture, text=True, check=True, timeout=120)
        except subprocess.CalledProcessError as e:
            if not quiet:
                logger.error("Remote command failed: %s", cmd_str)
                if capture and e.stderr:
                    logger.error("stderr: %s", e.stderr.strip())
            raise
        except subprocess.TimeoutExpired:
            logger.error("Remote command timed out: %s", cmd_str)
            raise

    def upload_file(self, local_path: str, remote_path: str) -> None:
        scp_cmd = [
            "scp", "-o", f"ControlPath={self.socket}", "-P", self.port,
            local_path, f"{self.user}@{self.host}:{remote_path}",
        ]
        logger.info("Uploading %s -> %s:%s", local_path, self.host, remote_path)
        try:
            subprocess.run(scp_cmd, capture_output=True, check=True, timeout=60)
        except subprocess.CalledProcessError as e:
            logger.error("Failed to upload file %s", local_path)
            if e.stderr:
                logger.error("scp stderr: %s", e.stderr.strip())
            raise


# ── Remote data fetching ─────────────────────────────────────────────────


def _run_cmd(orch: PVEOrchestrator, cmd_str: str, quiet: bool = False) -> str:
    """Run a remote command and return stdout, or '' on failure."""
    try:
        res = orch.run_remote(cmd_str, quiet=quiet)
        return res.stdout.strip() if res else ""
    except subprocess.CalledProcessError:
        return ""


def _run_pve_cmd(orch: PVEOrchestrator, cmd: str) -> str:
    """Run a PVE admin command (pct/qm), trying direct, sudo -n, then sudo -S with password."""
    raw = _run_cmd(orch, cmd, quiet=True)
    if raw:
        return raw

    raw = _run_cmd(orch, f"sudo -n {cmd} 2>/dev/null || true", quiet=True)
    if raw:
        return raw

    if orch.password:
        b64 = _to_b64(f"{orch.password}\n")
        raw = _run_cmd(orch, f"echo {b64} | base64 -d | sudo -S {cmd} 2>/dev/null || true", quiet=True)
        if raw:
            return raw

    return ""


def _check_pve_access(orch: PVEOrchestrator) -> tuple[bool, str]:
    """Check if we can run PVE admin commands and return (ok, hint)."""
    test = _run_pve_cmd(orch, "pveversion")
    if test:
        return True, ""
    whoami = _run_cmd(orch, "whoami", quiet=True)
    sudo_ok = _run_cmd(orch, "sudo -n true 2>/dev/null && echo OK", quiet=True)
    if whoami != "root" and "OK" not in sudo_ok:
        return False, f"User '{whoami}' needs root or passwordless sudo for PVE data"
    return False, "Unknown permission issue — try connecting as root"


def _fetch_server_resources(orch: PVEOrchestrator) -> dict:
    """Fetch host-level resource info: hostname, uptime, CPU, RAM, swap, disk, PVE version."""
    info = {}

    hostname = _run_cmd(orch, "hostname")
    uptime = _run_cmd(orch, "uptime -p | sed 's/^up //'")
    version = _run_cmd(orch, "pveversion 2>/dev/null || echo 'N/A'")
    cpu_cores = _run_cmd(orch, "nproc 2>/dev/null || echo '?'")
    cpu_model = _run_cmd(orch, "grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | sed 's/^ //'")

    info["hostname"] = hostname or "unknown"
    info["uptime"] = uptime or "N/A"
    info["version"] = version or "N/A"
    info["cpu_cores"] = cpu_cores
    info["cpu_model"] = cpu_model

    # RAM
    ram_raw = _run_cmd(orch, "free -h | awk 'NR==2{print $2, $3, $4, $7}'")
    if ram_raw:
        parts = ram_raw.split()
        info["ram_total"], info["ram_used"], info["ram_free"], info["ram_avail"] = parts[0], parts[1], parts[2], parts[3] if len(parts) >= 4 else "?"

    # Swap
    swap_raw = _run_cmd(orch, "free -h | awk 'NR==3{print $2, $3, $4}'")
    if swap_raw:
        parts = swap_raw.split()
        info["swap_total"], info["swap_used"], info["swap_free"] = parts[0], parts[1], parts[2]

    # Root disk
    disk_raw = _run_cmd(orch, "df -h / | awk 'NR==2{print $2, $3, $4}'")
    if disk_raw:
        parts = disk_raw.split()
        info["disk_total"], info["disk_used"], info["disk_avail"] = parts[0], parts[1], parts[2]

    return info


def _fetch_containers(orch: PVEOrchestrator) -> list[dict]:
    """Fetch LXC container list with per-container config details."""
    raw = _run_pve_cmd(orch, "pct list")
    if not raw:
        return []

    lines = raw.splitlines()
    if len(lines) < 2:
        return []

    containers = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        vmid = parts[0]
        status = parts[1]
        name = parts[2]

        config_raw = _run_pve_cmd(orch, f"pct config {vmid}")
        ct = {"vmid": vmid, "name": name, "status": status, "cores": "?", "memory": "?", "swap": "?", "ip": "?", "tags": "", "onboot": ""}

        for cline in config_raw.splitlines():
            if ":" in cline:
                key, _, val = cline.partition(":")
                val = val.strip()
                k = key.strip()
                if k == "cores":
                    ct["cores"] = val
                elif k == "memory":
                    ct["memory"] = val
                elif k == "swap":
                    ct["swap"] = val
                elif k in ("hostname", "host-name"):
                    ct["name"] = val
                elif k in ("ip", "net0"):
                    if "ip=" in val:
                        ip_part = val.split("ip=")[1].split("/")[0] if "ip=" in val else val
                        ct["ip"] = ip_part
                    else:
                        ct["ip"] = val
                elif k == "tags":
                    ct["tags"] = val
                elif k == "onboot":
                    ct["onboot"] = val

        containers.append(ct)

    return containers


def _fetch_vms(orch: PVEOrchestrator) -> list[dict]:
    """Fetch QEMU VM list with per-VM config details."""
    raw = _run_pve_cmd(orch, "qm list")
    if not raw:
        return []

    lines = raw.splitlines()
    if len(lines) < 2:
        return []

    vms = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        vmid = parts[0]
        name = parts[1]
        status = parts[2]

        config_raw = _run_pve_cmd(orch, f"qm config {vmid}")
        vm = {"vmid": vmid, "name": name, "status": status, "cores": "?", "memory": "?", "disk": "?"}

        for cline in config_raw.splitlines():
            if ":" in cline:
                key, _, val = cline.partition(":")
                val = val.strip()
                k = key.strip()
                if k == "cores":
                    vm["cores"] = val
                elif k == "memory":
                    vm["memory"] = val
                elif k.startswith("virtio"):
                    if "size=" in val:
                        disk_part = val.split("size=")[1].split(",")[0]
                        vm["disk"] = disk_part

        vms.append(vm)

    return vms


# ── Display ──────────────────────────────────────────────────────────────


def _fmt_ok(val: str) -> str:
    return f"\033[92m{val}\033[0m"


def _fmt_warn(val: str) -> str:
    return f"\033[93m{val}\033[0m"


def _fmt_dim(val: str) -> str:
    return f"\033[90m{val}\033[0m"


def _fmt_memory(val: str) -> str:
    """Convert MB value to human-readable GB, or return as-is if already formatted."""
    if not val or val == "?":
        return "?"
    try:
        mb = int(val)
        if mb >= 1024:
            gb = mb / 1024
            if gb == int(gb):
                return f"{int(gb)}G"
            return f"{gb:.1f}G"
        return f"{mb}M"
    except ValueError:
        return val


def _fmt_status(status: str) -> str:
    if status == "running":
        return _fmt_ok("running")
    elif status == "stopped":
        return _fmt_warn("stopped")
    return _fmt_dim(status)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a formatted table with unicode box-drawing."""
    col_count = len(headers)
    widths = [len(h) + 2 for h in headers]

    for row in rows:
        for i, cell in enumerate(row[:col_count]):
            # strip ANSI codes for width calculation
            clean = cell
            for code in ("\033[92m", "\033[93m", "\033[90m", "\033[0m"):
                clean = clean.replace(code, "")
            widths[i] = max(widths[i], len(clean) + 2)

    sep = "\u2500"
    top = "\u250c" + sep.join(sep * w for w in widths) + "\u2510"
    header_line = "\u2502" + "\u2502".join(h.center(w) for h, w in zip(headers, widths)) + "\u2502"
    mid = "\u251c" + sep.join(sep * w for w in widths) + "\u2524"
    bottom = "\u2514" + sep.join(sep * w for w in widths) + "\u2518"

    print(top)
    print(header_line)
    print(mid)

    for row in rows:
        cells = row[:col_count]
        ansi_offsets = []
        clean_cells = []
        for cell in cells:
            clean = cell
            offset = 0
            for code in ("\033[92m", "\033[93m", "\033[90m"):
                if code in clean:
                    offset += len(code)
            clean = clean.replace("\033[92m", "").replace("\033[93m", "").replace("\033[90m", "").replace("\033[0m", "")
            ansi_offsets.append(offset)
            clean_cells.append(clean)

        padded = []
        for i, (cell, clean, off) in enumerate(zip(cells, clean_cells, ansi_offsets)):
            w = widths[i]
            padding = w - len(clean) - off
            left_pad = padding // 2
            right_pad = padding - left_pad
            padded.append(" " * left_pad + cell + " " * right_pad)

        print("\u2502" + "\u2502".join(padded) + "\u2502")

    print(bottom)


def display_report(info: dict, containers: list[dict], vms: list[dict], access_hint: str = "") -> None:
    """Print full inspection report."""
    if access_hint:
        print(f"  {_fmt_warn('\u26a0')}  {access_hint}")
        print()

    # ── Server header ──
    host_label = f"{info.get('hostname', '?')} ({info.get('host_ip', '?')})"
    version = info.get("version", "?")
    uptime = info.get("uptime", "?")

    print()
    print(f"  {_fmt_ok('\u25a0')}  {host_label}")
    print(f"     PVE {version}  \u00b7  up {uptime}")
    print()

    # ── System resources ──
    print(f"  {_fmt_ok('\u2699')}  System Resources")
    print(f"     CPU:  {info.get('cpu_model', '?')}  ({info.get('cpu_cores', '?')} cores)")
    print(f"     RAM:  {info.get('ram_used', '?')} / {info.get('ram_total', '?')}  (avail: {info.get('ram_avail', '?')})")
    print(f"     SWAP: {info.get('swap_used', '?')} / {info.get('swap_total', '?')}")
    print(f"     DISK: {info.get('disk_used', '?')} / {info.get('disk_total', '?')}  (free: {info.get('disk_avail', '?')})")
    print()

    # ── Containers ──
    print(f"  {_fmt_ok('\u2630')}  Containers (LXC)  {_fmt_dim(f'({len(containers)} total)') if containers else _fmt_dim('(none)')}")
    if containers:
        headers = ["VMID", "Hostname", "Status", "CPU", "RAM", "Swap", "IP"]
        rows = []
        for ct in containers:
            rows.append([
                ct["vmid"],
                ct["name"],
                _fmt_status(ct["status"]),
                ct.get("cores", "?"),
                _fmt_memory(ct.get("memory", "?")),
                _fmt_memory(ct.get("swap", "?")),
                ct.get("ip", "?"),
            ])
        _print_table(headers, rows)
        print()

    # ── VMs ──
    print(f"  {_fmt_ok('\u25a3')}  Virtual Machines (QEMU)  {_fmt_dim(f'({len(vms)} total)') if vms else _fmt_dim('(none)')}")
    if vms:
        headers = ["VMID", "Name", "Status", "CPU", "RAM", "Disk"]
        rows = []
        for vm in vms:
            rows.append([
                vm["vmid"],
                vm["name"],
                _fmt_status(vm["status"]),
                vm.get("cores", "?"),
                _fmt_memory(vm.get("memory", "?")),
                vm.get("disk", "?"),
            ])
        _print_table(headers, rows)
        print()


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="bash-pve-utils: PVE Server Inspector"
    )
    parser.add_argument("host", nargs="?", default=None,
                        help="Proxmox VE server hostname or IP (omit for interactive mode)")
    parser.add_argument("--user", default="root", help="SSH user name (default: root)")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--password", action="store_true", help="Prompt for SSH password")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.host is None:
        creds = _prompt_credentials()
        host, user, port, password = creds["host"], creds["user"], creds["port"], creds["password"]
        logger.setLevel(logging.WARNING)
    else:
        host = args.host
        user = args.user
        port = args.port
        password = getpass.getpass("SSH Password (leave empty for key auth): ") if args.password else ""
        logger.setLevel(logging.WARNING)

    orch = PVEOrchestrator(host, user, port, password)

    try:
        orch.establish_connection()
        print()
        print(f"  {_fmt_ok('\u25b6')}  Connecting to {host}...")

        access_ok, access_hint = _check_pve_access(orch)

        print(f"  {_fmt_dim('\u25b6')}  Fetching server resources...")
        info = _fetch_server_resources(orch)
        info["host_ip"] = host

        if access_ok:
            print(f"  {_fmt_dim('\u25b6')}  Fetching containers...")
            containers = _fetch_containers(orch)
            print(f"  {_fmt_dim('\u25b6')}  Fetching VMs...")
            vms = _fetch_vms(orch)
        else:
            containers = []
            vms = []

        hint = f"Limited data: {access_hint}" if not access_ok else ""
        display_report(info, containers, vms, access_hint=hint)

        orch.close_connection()

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        orch.close_connection()
        sys.exit(130)

    print(f"  {_fmt_dim('Done.')}")
    print()


if __name__ == "__main__":
    main()
