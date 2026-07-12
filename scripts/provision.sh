#!/usr/bin/env bash
# bash-pve-utils: Remote LXC Provisioning Script
# This script is uploaded and executed on the Proxmox VE server.
set -euo pipefail

# ── Argument Parsing ────────────────────────────────────────────────────

VMID="${1}"
HOSTNAME="${2}"
CORES="${3}"
MEMORY="${4}"
SWAP="${5}"
DISK="${6}"
OSTEMPLATE="${7}"
BRIDGE="${8}"
IP="${9}"
GATEWAY="${10:-}"
SSH_KEY_B64="${11:-}"
ONBOOT="${12:-0}"
STORAGE="${13:-local-lvm}"
TEMPLATE_STORAGE="${14:-local}"
BOOTSTRAP_B64="${15:-}"

echo "[bash-pve-utils] Processing LXC Container VMID: ${VMID} (${HOSTNAME})"

# ── Helper: wait for container to be ready ───────────────────────────────

wait_container_ready() {
    local vmid="$1"
    local max_attempts="${2:-30}"
    echo "[bash-pve-utils] Waiting for container ${vmid} to be ready..."
    for i in $(seq 1 "${max_attempts}"); do
        if pct exec "${vmid}" -- echo "ready" >/dev/null 2>&1; then
            echo "[bash-pve-utils] Container ${vmid} ready after ${i}s"
            return 0
        fi
        sleep 1
    done
    echo "[bash-pve-utils] WARNING: Container ${vmid} did not become ready within ${max_attempts}s"
    return 1
}

# ── Helper: is container running? ────────────────────────────────────────

container_is_running() {
    local vmid="$1"
    pct status "${vmid}" 2>/dev/null | grep -q "status: running"
}

# ── Helper: decode b64 payload ──────────────────────────────────────────

decode_b64() {
    local encoded="$1"
    if [ -z "${encoded}" ]; then
        echo ""
        return
    fi
    echo "${encoded}" | base64 --decode 2>/dev/null || echo ""
}

# ── Main ─────────────────────────────────────────────────────────────────

# 1. Decode payloads
SSH_KEY=$(decode_b64 "${SSH_KEY_B64}")
BOOTSTRAP=$(decode_b64 "${BOOTSTRAP_B64}")

# 2. Template verification & auto-download
BASENAME=$(basename "${OSTEMPLATE}")
TEMPLATE_PATH="${TEMPLATE_STORAGE}:vztmpl/${BASENAME}"
echo "[bash-pve-utils] Checking template: ${TEMPLATE_PATH}"

if ! pveam list "${TEMPLATE_STORAGE}" | grep -Fq "${BASENAME}"; then
    echo "[bash-pve-utils] Template not found in storage '${TEMPLATE_STORAGE}'. Downloading..."
    pveam download "${TEMPLATE_STORAGE}" "${OSTEMPLATE}"
else
    echo "[bash-pve-utils] Template already cached."
fi

# 3. Check if container exists
if pct status "${VMID}" >/dev/null 2>&1; then
    echo "[bash-pve-utils] Container ${VMID} already exists. Checking for updates..."

    # Read current configuration
    CURRENT_CORES=$(pct config "${VMID}" | grep -E '^cores:' | awk '{print $2}' || echo "")
    CURRENT_MEMORY=$(pct config "${VMID}" | grep -E '^memory:' | awk '{print $2}' || echo "")
    CURRENT_SWAP=$(pct config "${VMID}" | grep -E '^swap:' | awk '{print $2}' || echo "")
    CURRENT_ONBOOT=$(pct config "${VMID}" | grep -E '^onboot:' | awk '{print $2}' || echo "0")

    [ -z "${CURRENT_CORES}" ] && CURRENT_CORES="1"
    [ -z "${CURRENT_MEMORY}" ] && CURRENT_MEMORY="512"
    [ -z "${CURRENT_SWAP}" ] && CURRENT_SWAP="512"

    UPDATES=()

    if [ "${CURRENT_CORES}" != "${CORES}" ]; then
        echo "[bash-pve-utils] CPU cores update: ${CURRENT_CORES} -> ${CORES}"
        UPDATES+=("-cores" "${CORES}")
    fi

    if [ "${CURRENT_MEMORY}" != "${MEMORY}" ]; then
        echo "[bash-pve-utils] Memory update: ${CURRENT_MEMORY} MB -> ${MEMORY} MB"
        UPDATES+=("-memory" "${MEMORY}")
    fi

    if [ "${CURRENT_SWAP}" != "${SWAP}" ]; then
        echo "[bash-pve-utils] Swap update: ${CURRENT_SWAP} MB -> ${SWAP} MB"
        UPDATES+=("-swap" "${SWAP}")
    fi

    if [ "${CURRENT_ONBOOT}" != "${ONBOOT}" ]; then
        echo "[bash-pve-utils] Onboot update: ${CURRENT_ONBOOT} -> ${ONBOOT}"
        UPDATES+=("-onboot" "${ONBOOT}")
    fi

    if [ ${#UPDATES[@]} -gt 0 ]; then
        echo "[bash-pve-utils] Applying configuration updates to ${VMID}..."

        # Try hotplug first; if it fails, stop → update → start
        if ! pct set "${VMID}" "${UPDATES[@]}" 2>/dev/null; then
            echo "[bash-pve-utils] Hotplug failed, using stop→update→start cycle..."
            WAS_RUNNING=false
            if container_is_running "${VMID}"; then
                WAS_RUNNING=true
                echo "[bash-pve-utils] Stopping container ${VMID}..."
                pct stop "${VMID}"
            fi
            pct set "${VMID}" "${UPDATES[@]}"
            if [ "${WAS_RUNNING}" = true ]; then
                echo "[bash-pve-utils] Starting container ${VMID}..."
                pct start "${VMID}"
            fi
        fi
        echo "[bash-pve-utils] Container ${VMID} updated successfully."
    else
        echo "[bash-pve-utils] Container ${VMID} is up to date. No changes needed."
    fi
else
    echo "[bash-pve-utils] Container ${VMID} does not exist. Creating new LXC..."

    # ── Create container ──────────────────────────────────────────────

    NET_ARGS="name=eth0,bridge=${BRIDGE},ip=${IP}"
    if [ -n "${GATEWAY}" ]; then
        NET_ARGS="${NET_ARGS},gw=${GATEWAY}"
    fi

    SSH_KEY_FILE=""
    if [ -n "${SSH_KEY}" ]; then
        SSH_KEY_FILE=$(mktemp)
        echo "${SSH_KEY}" > "${SSH_KEY_FILE}"
    fi

    pct create "${VMID}" "${TEMPLATE_PATH}" \
        -hostname "${HOSTNAME}" \
        -cores "${CORES}" \
        -memory "${MEMORY}" \
        -swap "${SWAP}" \
        -rootfs "${STORAGE}:${DISK}" \
        -net0 "${NET_ARGS}" \
        -onboot "${ONBOOT}" \
        -unprivileged 1 \
        ${SSH_KEY_FILE:+-ssh-public-keys "${SSH_KEY_FILE}"}

    echo "[bash-pve-utils] Container ${VMID} created successfully."

    if [ -n "${SSH_KEY_FILE}" ]; then
        rm -f "${SSH_KEY_FILE}"
    fi

    # ── Bootstrap ─────────────────────────────────────────────────────

    if [ -n "${BOOTSTRAP}" ]; then
        echo "[bash-pve-utils] Running bootstrap scripts..."

        WAS_RUNNING=false
        if container_is_running "${VMID}"; then
            WAS_RUNNING=true
        else
            echo "[bash-pve-utils] Starting container for bootstrap..."
            pct start "${VMID}"
        fi

        # Wait for container to be ready (replaces hardcoded sleep 5)
        wait_container_ready "${VMID}"

        # Run bootstrap commands
        echo "${BOOTSTRAP}" | pct exec "${VMID}" -- sh
        echo "[bash-pve-utils] Bootstrap completed."

        if [ "${WAS_RUNNING}" = false ]; then
            echo "[bash-pve-utils] Stopping container..."
            pct stop "${VMID}"
        fi
    fi
fi
