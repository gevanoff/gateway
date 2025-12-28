#!/usr/bin/env bash
set -euo pipefail

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

if [[ "$(uname -s 2>/dev/null || echo unknown)" != "Darwin" ]]; then
  echo "ERROR: This deploy script targets macOS (launchd)." >&2
  echo "Hint: run it on the Mac host that runs launchd for ${LAUNCHD_LABEL:-the gateway}." >&2
  exit 1
fi

require_cmd sudo
require_cmd rsync
require_cmd launchctl
require_cmd curl
require_cmd lsof
require_cmd sed
require_cmd tail

# ---- config (edit if your labels/paths differ) ----
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="/var/lib/gateway"
APP_DIR="${RUNTIME_ROOT}/app"
LAUNCHD_LABEL="com.ai.gateway"
PLIST="/Library/LaunchDaemons/${LAUNCHD_LABEL}.plist"
HEALTH_URL="http://127.0.0.1:8800/health"
PORT="8800"
LOG_DIR="/var/log/gateway"
ERR_LOG="${LOG_DIR}/gateway.err.log"
OUT_LOG="${LOG_DIR}/gateway.out.log"
PYTHON_BIN="${RUNTIME_ROOT}/env/bin/python"
CURL_CONNECT_TIMEOUT_SEC="1"
CURL_MAX_TIME_SEC="2"

# ---- safety checks ----
if [[ ! -d "${REPO_ROOT}/.git" ]]; then
  echo "ERROR: ${REPO_ROOT} does not look like a git repo (missing .git)" >&2
  exit 1
fi

if [[ ! -d "${RUNTIME_ROOT}" ]]; then
  echo "ERROR: runtime root ${RUNTIME_ROOT} does not exist" >&2
  echo "Hint: create it with: sudo mkdir -p ${RUNTIME_ROOT}" >&2
  exit 1
fi

echo "Repo:    ${REPO_ROOT}"
echo "Deploy:  ${APP_DIR}"
echo "Label:   ${LAUNCHD_LABEL}"

if ! id -u gateway >/dev/null 2>&1; then
  echo "ERROR: user 'gateway' does not exist on this machine" >&2
  echo "Hint: create it (or change chown target in this script)." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ERROR: expected python not found/executable: ${PYTHON_BIN}" >&2
  echo "Hint: the plist runs ${PYTHON_BIN} -m uvicorn from /var/lib/gateway/app" >&2
  echo "Hint: create the venv at /var/lib/gateway/env and install deps." >&2
  exit 1
fi

# ---- ensure runtime layout expected by app ----
# main.py reads env from /var/lib/gateway/app/.env, uses /var/lib/gateway/tools, and writes /var/lib/gateway/data/memory.sqlite
sudo mkdir -p "${RUNTIME_ROOT}/data" "${RUNTIME_ROOT}/tools"
sudo chown -R gateway:staff "${RUNTIME_ROOT}/data" "${RUNTIME_ROOT}/tools"
sudo chmod -R u+rwX,go-rwx "${RUNTIME_ROOT}/data" "${RUNTIME_ROOT}/tools"

# launchd won't create parent log directories; the plist points stdout/stderr into /var/log/gateway
sudo mkdir -p "${LOG_DIR}"
sudo chown -R gateway:staff "${LOG_DIR}"
sudo chmod -R u+rwX,go-rwx "${LOG_DIR}"

# ---- deploy code (exclude runtime/state/dev noise) ----
# NOTE: trailing slashes matter: sync repo CONTENTS into app dir
sudo mkdir -p "${APP_DIR}"
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude '.gitignore' \
  --exclude '.DS_Store' \
  --exclude '.env' --exclude '.env.*' \
  --exclude 'env/' --exclude '.venv/' --exclude 'venv/' \
  --exclude 'data/' --exclude '*.sqlite' --exclude '*.sqlite3' --exclude '*.db' --exclude '*.wal' --exclude '*.shm' \
  --exclude 'logs/' --exclude '*.log' \
  --exclude 'cache/' --exclude 'models/' --exclude 'huggingface/' --exclude 'hf_cache/' \
  "${REPO_ROOT}/" "${APP_DIR}/"

# ---- permissions ----
sudo chown -R gateway:staff "${APP_DIR}"
sudo chmod -R u+rwX,go-rwx "${APP_DIR}"

# ---- restart service ----
# kickstart alone is fine if it is already bootstrapped; bootstrap if missing.
if sudo launchctl print "system/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
  sudo launchctl kickstart -k "system/${LAUNCHD_LABEL}"
else
  if [[ ! -f "${PLIST}" ]]; then
    echo "ERROR: plist not found at ${PLIST}" >&2
    exit 1
  fi
  sudo launchctl bootstrap system "${PLIST}"
  sudo launchctl kickstart -k "system/${LAUNCHD_LABEL}"
fi

# ---- verify ----
echo "Waiting for health endpoint..."
for i in {1..30}; do
  # Add explicit timeouts so a stalled connect/read can't hang the deploy.
  if curl -fsS --connect-timeout "${CURL_CONNECT_TIMEOUT_SEC}" --max-time "${CURL_MAX_TIME_SEC}" "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "OK: health endpoint responds"
    break
  fi
  sleep 0.2
  if [[ $i -eq 30 ]]; then
    echo "ERROR: health check failed: ${HEALTH_URL}" >&2
    echo "---- launchctl state ----"
    sudo launchctl print "system/${LAUNCHD_LABEL}" | sed -n '1,200p' || true
    echo "---- recent stderr ----"
    sudo tail -n 200 "${ERR_LOG}" 2>/dev/null || true
    echo "---- recent stdout ----"
    sudo tail -n 200 "${OUT_LOG}" 2>/dev/null || true
    exit 1
  fi
done

echo "Checking port ${PORT}..."
sudo lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN || true

echo "Deploy complete."
