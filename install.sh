#!/usr/bin/env bash
# ==========================================================================
#  Panah VPN Panel — one click installer
#  ------------------------------------------------------------------------
#  Usage:
#     curl -fsSL https://raw.githubusercontent.com/farnoudhosseini/vpn-panel/main/install.sh | sudo bash
#
#     ... or, with the answers supplied up front:
#     sudo ./install.sh --mode panel --domain vpn.example.com --email me@example.com --port 443
#     sudo ./install.sh --mode node  --agent-key <key from the panel>
#
#  The script is idempotent: running it again upgrades the checkout, rewrites
#  the environment file and restarts the service without touching data.
# ==========================================================================
set -Eeuo pipefail

VERSION="1.0.0"
PRODUCT="Panah"
REPO_URL="${PANAH_REPO:-https://github.com/farnoudhosseini/vpn-panel.git}"
BRANCH="${PANAH_BRANCH:-main}"

SRC_DIR="/opt/panah/src"
VENV_DIR="/opt/panah/venv"
DATA_DIR="/opt/panah/data"
CONFIG_DIR="/etc/panah"
SECRETS_DIR="/etc/panah/secrets"
ENV_FILE="${CONFIG_DIR}/panel.env"
LOG_DIR="/var/log/panah"
WEB_ROOT="/var/www/panah"
PANEL_SERVICE="panah.service"
AGENT_SERVICE="panah-agent.service"
BACKPACK_BIN="/usr/local/bin/backpack"

# --------------------------------------------------------------------------
#  Options
# --------------------------------------------------------------------------
MODE=""
DOMAIN=""
EMAIL=""
PUBLIC_PORT="443"
INTERNAL_PORT="8080"
AGENT_PORT="8787"
AGENT_KEY=""
ASSUME_YES=0
WANT_CERTBOT=1
WANT_BACKPACK=1
WANT_FIREWALL=0
SKIP_FRONTEND=0
NON_INTERACTIVE=0
PORT_FROM_FLAG=0
AGENT_PORT_FROM_FLAG=0

LOG_FILE=""
TTY=0
[[ -t 1 ]] && TTY=1

# --------------------------------------------------------------------------
#  Presentation — an install log that reads like a build, not like a dump
# --------------------------------------------------------------------------
if [[ $TTY -eq 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_RED=$'\033[38;5;203m'; C_GREEN=$'\033[38;5;78m'
  C_AMBER=$'\033[38;5;215m'; C_BLUE=$'\033[38;5;110m'
else
  C_RESET=""; C_DIM=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_AMBER=""; C_BLUE=""
fi

STEP_TOTAL=14
STEP_INDEX=0

hr() { printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────────${C_RESET}"; }

banner() {
  printf '\n%s' "$C_RED"
  cat <<'ASCII'
   ██████╗  █████╗ ███╗   ██╗ █████╗ ██╗  ██╗
   ██╔══██╗██╔══██╗████╗  ██║██╔══██╗██║  ██║
   ██████╔╝███████║██╔██╗ ██║███████║███████║
   ██╔═══╝ ██╔══██║██║╚██╗██║██╔══██║██╔══██║
   ██║     ██║  ██║██║ ╚████║██║  ██║██║  ██║
   ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝  ╚═╝
ASCII
  printf '%s' "$C_RESET"
  printf '   %sVPN control plane — installer %s%s   %s[panel | node]%s\n' "$C_BOLD" "$VERSION" "$C_RESET" "$C_DIM" "$C_RESET"
  hr
}

step() {
  STEP_INDEX=$((STEP_INDEX + 1))
  printf '\n%s▸ %d/%d%s %s%s%s\n' "$C_RED" "$STEP_INDEX" "$STEP_TOTAL" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"
}
ok()   { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
note() { printf '  %s·%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_DIM" "$*" "$C_RESET"; }
warn() { printf '  %s▲%s %s\n' "$C_AMBER" "$C_RESET" "$*"; }
fail() {
  printf '\n  %s✘ %s%s\n' "$C_RED" "$*" "$C_RESET"
  if [[ -n "$LOG_FILE" && -s "$LOG_FILE" ]]; then
    printf '\n  %s--- last 25 log lines ---%s\n' "$C_DIM" "$C_RESET"
    tail -n 25 "$LOG_FILE" | sed 's/^/    /'
    printf '  %s--- full log: %s ---%s\n' "$C_DIM" "$LOG_FILE" "$C_RESET"
  fi
  exit 1
}

logline() { if [[ -n "$LOG_FILE" ]]; then printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*" >>"$LOG_FILE"; fi; }

on_error() {
  local exit_code=$?
  printf '\n  %s✘ failed at line %s (exit %s)%s\n' "$C_RED" "${BASH_LINENO[0]:-?}" "$exit_code" "$C_RESET"
  exit "$exit_code"
}
trap on_error ERR

# Run a command with a spinner. Output goes to the install log; the tail is
# shown only when the command fails.
spin() {
  local label="$1"; shift
  logline "\$ $*"
  local started=$SECONDS pid status=0 i=0
  local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
  "$@" >>"$LOG_FILE" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s%s %s %s(%ss)%s ' "$C_RED" "${frames[$((i % 10))]}" "$C_RESET" "$label" "$C_DIM" "$((SECONDS - started))" "$C_RESET"
    i=$((i + 1))
    sleep 0.09
  done
  wait "$pid" || status=$?
  printf '\r\033[K'
  (( status == 0 )) || fail "$label failed"
  ok "$label"
}

# A long command shown with a sweeping progress bar.
progress() {
  local label="$1"; shift
  logline "\$ $*"
  local started=$SECONDS pid status=0 width=28 pos=0 bar j
  "$@" >>"$LOG_FILE" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    bar=""
    for ((j = 0; j < width; j++)); do
      if (( j >= pos && j < pos + 6 )); then bar+="█"; else bar+="░"; fi
    done
    printf '\r  %s▏%s▕%s %s %s(%ss)%s ' "$C_RED" "$bar" "$C_RESET" "$label" "$C_DIM" "$((SECONDS - started))" "$C_RESET"
    pos=$(((pos + 1) % (width + 6)))
    sleep 0.08
  done
  wait "$pid" || status=$?
  printf '\r\033[K'
  (( status == 0 )) || fail "$label failed"
  ok "$label"
}

ask() { # ask <prompt> <default> <varname>
  local prompt="$1" default="$2" var="$3" answer=""
  if [[ $NON_INTERACTIVE -eq 1 ]]; then
    printf -v "$var" '%s' "$default"
    return
  fi
  if [[ -n "$default" ]]; then
    read -r -p "  ${C_BOLD}${prompt}${C_RESET} ${C_DIM}[${default}]${C_RESET}: " answer </dev/tty || answer=""
  else
    read -r -p "  ${C_BOLD}${prompt}${C_RESET}: " answer </dev/tty || answer=""
  fi
  [[ -z "$answer" ]] && answer="$default"
  printf -v "$var" '%s' "$answer"
}

ask_secret() {
  local prompt="$1" var="$2" answer=""
  read -r -s -p "  ${C_BOLD}${prompt}${C_RESET}: " answer </dev/tty || answer=""
  printf '\n'
  printf -v "$var" '%s' "$answer"
}

confirm() { # confirm <prompt> [default: y|n]
  local prompt="$1" default="${2:-y}" answer=""
  if [[ $ASSUME_YES -eq 1 || $NON_INTERACTIVE -eq 1 ]]; then return 0; fi
  local hint="[Y/n]"
  [[ "$default" == "n" ]] && hint="[y/N]"
  read -r -p "  ${C_BOLD}${prompt}${C_RESET} ${C_DIM}${hint}${C_RESET}: " answer </dev/tty || answer=""
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[Yy]$ ]]
}

usage() {
  cat <<EOF
${PRODUCT} installer ${VERSION}

  --mode panel|node       what to install (asked when omitted)
  --domain <name>         public hostname for the panel (panel mode)
  --email <address>       contact address for Let's Encrypt (panel mode)
  --port <port>           public HTTPS port for the panel (default ${PUBLIC_PORT})
  --internal-port <port>  loopback port uvicorn binds (default ${INTERNAL_PORT})
  --agent-key <key>       shared key the master panel uses (node mode)
  --agent-port <port>     agent listen port (default ${AGENT_PORT})
  --no-certbot            skip Let's Encrypt and use a self-signed certificate
  --no-backpack           do not install the BackPack tunnel binary
  --skip-frontend         do not build the web interface (API only)
  --firewall              open the required ports with ufw when it is active
  --repo <url> --branch <name>
  -y, --yes               assume yes for every question
  --non-interactive       never prompt; use defaults (requires --mode)
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --port) PUBLIC_PORT="${2:-}"; PORT_FROM_FLAG=1; shift 2 ;;
    --internal-port) INTERNAL_PORT="${2:-}"; shift 2 ;;
    --agent-key) AGENT_KEY="${2:-}"; shift 2 ;;
    --agent-port) AGENT_PORT="${2:-}"; AGENT_PORT_FROM_FLAG=1; shift 2 ;;
    --no-certbot) WANT_CERTBOT=0; shift ;;
    --no-backpack) WANT_BACKPACK=0; shift ;;
    --skip-frontend) SKIP_FRONTEND=1; shift ;;
    --firewall) WANT_FIREWALL=1; shift ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n\n' "$1" >&2; usage; exit 2 ;;
  esac
done

# --------------------------------------------------------------------------
#  Small helpers
# --------------------------------------------------------------------------
gen_secret() { openssl rand -hex 32; }
valid_domain() { [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; }
valid_email()  { [[ "$1" =~ ^[^@[:space:]]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }
valid_port()   { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )); }
port_in_use()  { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }

# Secrets are written with the most restrictive permissions that still let the
# service read them.
write_secret() { # write_secret <path> <value>
  local path="$1" value="$2" previous
  previous="$(umask)"
  umask 077
  printf '%s' "$value" >"$path"
  chmod 0600 "$path"
  umask "$previous"
}

download() { # download <url> <destination>
  curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 240 -o "$2" "$1" 2>>"$LOG_FILE"
}

# Best-effort local address, used only for the summary. Never fails the run.
primary_ip() {
  local ip=""
  ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)" || ip=""
  if [[ -z "$ip" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || ip=""
  fi
  printf '%s' "${ip:-<server-ip>}"
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    printf '%sThis installer must run as root (try: sudo bash install.sh)%s\n' "$C_RED" "$C_RESET" >&2
    exit 1
  fi
}

# --------------------------------------------------------------------------
#  0. Preconditions
# --------------------------------------------------------------------------
require_root
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/install-$(date -u '+%Y%m%d-%H%M%S').log"
: >"$LOG_FILE" || fail "cannot write the install log at ${LOG_FILE}"

banner

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_VER="${VERSION_ID:-unknown}"
else
  OS_ID="unknown"; OS_VER="unknown"
fi
note "host: ${OS_ID} ${OS_VER} ($(uname -m))"

if [[ "$OS_ID" != "ubuntu" && "$OS_ID" != "debian" ]]; then
  warn "this installer targets Ubuntu 22.04/24.04; ${OS_ID} is untested"
  confirm "continue anyway?" "n" || exit 1
elif [[ "$OS_VER" != 24.04* && "$OS_VER" != 22.04* && "$OS_VER" != 12* ]]; then
  warn "tested on Ubuntu 22.04/24.04; this host reports ${OS_ID} ${OS_VER}"
fi

if [[ ! -r /dev/tty && $NON_INTERACTIVE -eq 0 ]]; then
  warn "no terminal available; continuing with defaults"
  NON_INTERACTIVE=1
  ASSUME_YES=1
fi

if [[ -z "$MODE" ]]; then
  printf '\n  %sWhat should this server be?%s\n' "$C_BOLD" "$C_RESET"
  printf '    %s1)%s Panel  — the master: web UI, database, node and tunnel control\n' "$C_RED" "$C_RESET"
  printf '    %s2)%s Node   — lightweight: agent, OpenVPN, nginx (no panel, no UI)\n\n' "$C_RED" "$C_RESET"
  ask "choose 1 or 2" "1" MODE_CHOICE
  case "$MODE_CHOICE" in
    1|panel|master) MODE="panel" ;;
    2|node|agent) MODE="node" ;;
    *) fail "unknown choice: ${MODE_CHOICE}" ;;
  esac
fi
[[ "$MODE" == "panel" || "$MODE" == "node" ]] || fail "--mode must be 'panel' or 'node'"
note "mode: ${MODE}"

# --------------------------------------------------------------------------
#  1. Answers
# --------------------------------------------------------------------------
if [[ "$MODE" == "panel" ]]; then
  if [[ -z "$DOMAIN" ]]; then
    DETECTED_IP="$(curl -4 -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
    printf '\n  %sDomain%s — the hostname whose A record already points at this server\n' "$C_BOLD" "$C_RESET"
    if [[ -n "$DETECTED_IP" ]]; then note "this server's public address looks like ${DETECTED_IP}"; fi
    ask "domain" "" DOMAIN
  fi
  valid_domain "$DOMAIN" || fail "invalid domain: ${DOMAIN}"

  if [[ -z "$EMAIL" ]]; then
    ask "email for Let's Encrypt notices" "admin@${DOMAIN}" EMAIL
  fi
  valid_email "$EMAIL" || fail "invalid email: ${EMAIL}"

  if [[ $PORT_FROM_FLAG -eq 0 ]]; then
    ask "public HTTPS port for the panel" "$PUBLIC_PORT" PUBLIC_PORT
  fi
  valid_port "$PUBLIC_PORT" || fail "invalid port: ${PUBLIC_PORT}"

  if port_in_use "$PUBLIC_PORT" && ! systemctl is-active --quiet "${PANEL_SERVICE}" 2>/dev/null; then
    warn "port ${PUBLIC_PORT} is already in use by another service"
    confirm "continue anyway?" "n" || exit 1
  fi
  if port_in_use 80 && ! systemctl is-active --quiet nginx 2>/dev/null; then
    warn "port 80 is busy; the Let's Encrypt HTTP-01 challenge may fail"
  fi

  if [[ $WANT_CERTBOT -eq 1 ]]; then
    confirm "obtain a Let's Encrypt certificate for ${DOMAIN}?" "y" || WANT_CERTBOT=0
  fi
else
  if [[ -z "$AGENT_KEY" ]]; then
    printf '\n  %sAgent key%s — the shared key the panel authenticates with.\n' "$C_BOLD" "$C_RESET"
    note "copy it from the panel installer output, or run 'panah doctor' on the panel host"
    ask_secret "agent key (empty = generate one now)" AGENT_KEY
    if [[ -z "$AGENT_KEY" ]]; then
      AGENT_KEY="$(gen_secret)"
      GENERATED_AGENT_KEY=1
    fi
  fi
  [[ ${#AGENT_KEY} -ge 16 ]] || fail "the agent key looks too short (minimum 16 characters)"
  if [[ $AGENT_PORT_FROM_FLAG -eq 0 ]]; then
    ask "agent port" "$AGENT_PORT" AGENT_PORT
  fi
  valid_port "$AGENT_PORT" || fail "invalid agent port: ${AGENT_PORT}"
fi

# --------------------------------------------------------------------------
#  2. Packages
# --------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
PKGS=(ca-certificates curl git unzip openssl gnupg python3 python3-venv python3-pip
      openvpn iproute2 iptables wireguard-tools procps nginx)
if [[ "$MODE" == "panel" ]]; then PKGS+=(sqlite3); fi
if [[ $WANT_CERTBOT -eq 1 ]]; then PKGS+=(certbot); fi

step "Installing system packages"
progress "apt update" apt-get update -qq
progress "apt install (${#PKGS[@]} packages)" apt-get install -y -qq --no-install-recommends "${PKGS[@]}"
for bin in python3 openssl git nginx; do
  command -v "$bin" >/dev/null 2>&1 || fail "${bin} is still missing after installation"
done
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
note "python ${PY_VERSION} · openvpn $(openvpn --version 2>/dev/null | head -1 | awk '{print $2}' || echo missing) · nginx $(nginx -v 2>&1 | sed -n 's|.*nginx/||p')"

if [[ "$MODE" == "panel" && $SKIP_FRONTEND -eq 0 ]]; then
  need_node=0
  if ! command -v node >/dev/null 2>&1; then
    need_node=1
  else
    node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    (( node_major >= 20 )) || need_node=1
  fi
  if (( need_node == 1 )); then
    step "Installing Node.js 22 for the web build"
    spin "nodesource keyring" bash -c 'curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg'
    spin "nodesource repository" bash -c 'echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list'
    spin "apt update" apt-get update -qq
    progress "install nodejs" apt-get install -y -qq nodejs
  fi
  command -v node >/dev/null 2>&1 || fail "node is required to build the web interface (use --skip-frontend to skip)"
  note "node $(node -v) · npm $(npm -v)"
fi

# --------------------------------------------------------------------------
#  3. Source checkout
# --------------------------------------------------------------------------
step "Fetching Panah ${VERSION}"
mkdir -p "$(dirname "$SRC_DIR")"
if [[ -d "${SRC_DIR}/.git" ]]; then
  spin "update ${SRC_DIR}" git -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH"
  spin "checkout ${BRANCH}" git -C "$SRC_DIR" reset --hard "origin/${BRANCH}"
else
  rm -rf "$SRC_DIR"
  spin "clone ${REPO_URL}" git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
fi
[[ -f "${SRC_DIR}/requirements.txt" ]] || fail "the checkout does not look like Panah (requirements.txt missing)"
note "revision $(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# --------------------------------------------------------------------------
#  4. Python environment
# --------------------------------------------------------------------------
step "Preparing the Python environment"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  spin "create virtualenv" python3 -m venv "$VENV_DIR"
fi
progress "upgrade pip" "${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools wheel
progress "install requirements" "${VENV_DIR}/bin/pip" install --quiet -r "${SRC_DIR}/requirements.txt"
"${VENV_DIR}/bin/python" -c 'import fastapi, sqlalchemy, cryptography, uvicorn' || fail "python dependencies did not import"

# The `panah` command: keeps the environment file loaded so the CLI behaves the
# same way the service does.
cat >"${VENV_DIR}/bin/panah" <<EOF
#!/bin/sh
set -e
export PYTHONPATH="${SRC_DIR}\${PYTHONPATH:+:\$PYTHONPATH}"
if [ -r "${ENV_FILE}" ]; then
  set -a
  . "${ENV_FILE}"
  set +a
fi
cd "${SRC_DIR}"
exec "${VENV_DIR}/bin/python" -m panel.cli "\$@"
EOF
chmod 0755 "${VENV_DIR}/bin/panah"
ln -sf "${VENV_DIR}/bin/panah" /usr/local/bin/panah
ok "panah CLI available at /usr/local/bin/panah"

# --------------------------------------------------------------------------
#  5. Directories
# --------------------------------------------------------------------------
step "Creating directories"
spin "filesystem layout" bash -c "
  set -e
  install -d -m 0750 '${DATA_DIR}' '${CONFIG_DIR}' '${SECRETS_DIR}' '${LOG_DIR}'
  install -d -m 0755 '${WEB_ROOT}/decoy' '/etc/openvpn/panah' '/etc/backpack'
  [ -d /etc/wireguard ] || install -d -m 0700 /etc/wireguard
  chmod 0700 '${SECRETS_DIR}'
"

# --------------------------------------------------------------------------
#  6. Environment file
# --------------------------------------------------------------------------
step "Writing ${ENV_FILE}"
if [[ -f "$ENV_FILE" ]]; then
  warn "existing configuration found; secrets and data are kept"
  SECRET_KEY="$(sed -n 's/^PANAH_SECRET_KEY=//p' "$ENV_FILE" | head -1)"
  EXISTING_AGENT_KEY="$(sed -n 's/^PANAH_AGENT_KEY=//p' "$ENV_FILE" | head -1)"
  EXISTING_DOMAIN="$(sed -n 's/^PANAH_DOMAIN=//p' "$ENV_FILE" | head -1)"
  EXISTING_INTERNAL_PORT="$(sed -n 's/^PANAH_PORT=//p' "$ENV_FILE" | head -1)"
else
  SECRET_KEY=""; EXISTING_AGENT_KEY=""; EXISTING_DOMAIN=""; EXISTING_INTERNAL_PORT=""
fi

if [[ "$MODE" == "panel" ]]; then
  PANEL_AGENT_KEY="$EXISTING_AGENT_KEY"
  [[ -n "$PANEL_AGENT_KEY" ]] || PANEL_AGENT_KEY="$(gen_secret)"
  if [[ -n "$EXISTING_DOMAIN" ]]; then DOMAIN="$EXISTING_DOMAIN"; fi
  if [[ -n "$EXISTING_INTERNAL_PORT" ]]; then INTERNAL_PORT="$EXISTING_INTERNAL_PORT"; fi
  if [[ "$PUBLIC_PORT" == "443" ]]; then ORIGIN="https://${DOMAIN}"; else ORIGIN="https://${DOMAIN}:${PUBLIC_PORT}"; fi

  umask 077
  cat >"$ENV_FILE" <<EOF
# Panah panel — generated by install.sh $(date -u '+%Y-%m-%dT%H:%M:%SZ')
# Re-running the installer preserves this file. After editing, run:
#   systemctl restart ${PANEL_SERVICE}
PANAH_MODE=panel
PANAH_DOMAIN=${DOMAIN}
PANAH_HOST=127.0.0.1
PANAH_PORT=${INTERNAL_PORT}
PANAH_DATA_DIR=${DATA_DIR}
PANAH_LOG_DIR=${LOG_DIR}
PANAH_AGENT_PORT=${AGENT_PORT}
PANAH_ALLOWED_ORIGINS=${ORIGIN}
PANAH_ACCESS_TOKEN_MINUTES=30
PANAH_REFRESH_TOKEN_DAYS=7
PANAH_COOKIE_SECURE=1
PANAH_ADMIN_USERNAME=admin
# A node server must use this same key, otherwise the panel cannot reach it.
PANAH_AGENT_KEY=${PANEL_AGENT_KEY}
PANAH_TRUSTED_PROXIES=127.0.0.1,::1
PANAH_RATE_LIMIT_PER_MINUTE=240
PANAH_MAX_LOGIN_ATTEMPTS=5
PANAH_LOGIN_LOCK_SECONDS=900
EOF
  umask 022
  chmod 0600 "$ENV_FILE"
  write_secret "$SECRETS_DIR/agent_key" "$PANEL_AGENT_KEY"
  if [[ -n "$SECRET_KEY" ]]; then write_secret "$SECRETS_DIR/secret_key" "$SECRET_KEY"; fi
  ok "configuration written; node servers will use this agent key"
  NODE_INSTALL_CMD="curl -fsSL https://raw.githubusercontent.com/farnoudhosseini/vpn-panel/${BRANCH}/install.sh | sudo bash -s -- --mode node --agent-key ${PANEL_AGENT_KEY}"
else
  NODE_KEY="${AGENT_KEY:-${EXISTING_AGENT_KEY:-}}"
  [[ -n "$NODE_KEY" ]] || fail "an agent key is required on a node"
  umask 077
  cat >"$ENV_FILE" <<EOF
# Panah node agent — generated by install.sh $(date -u '+%Y-%m-%dT%H:%M:%SZ')
PANAH_MODE=node
PANAH_HOST=0.0.0.0
PANAH_AGENT_PORT=${AGENT_PORT}
PANAH_DATA_DIR=${DATA_DIR}
PANAH_LOG_DIR=${LOG_DIR}
PANAH_AGENT_KEY=${NODE_KEY}
PANAH_LOG_LEVEL=INFO
EOF
  umask 022
  chmod 0600 "$ENV_FILE"
  write_secret "$SECRETS_DIR/agent_key" "$NODE_KEY"
  ok "configuration written"
fi

# --------------------------------------------------------------------------
#  7. Web interface
# --------------------------------------------------------------------------
if [[ "$MODE" == "panel" && $SKIP_FRONTEND -eq 0 ]]; then
  step "Building the web interface"
  progress "npm install" bash -c "cd '${SRC_DIR}/web' && { npm ci --no-audit --no-fund || npm install --no-audit --no-fund; }"
  progress "vite build" bash -c "cd '${SRC_DIR}/web' && npm run build"
  [[ -f "${SRC_DIR}/web/dist/index.html" ]] || fail "the frontend build produced no index.html"
  spin "publish to panel/static" bash -c "rm -rf '${SRC_DIR}/panel/static' && cp -r '${SRC_DIR}/web/dist' '${SRC_DIR}/panel/static' && chmod -R a+rX '${SRC_DIR}/panel/static'"
else
  step "Web interface"
  note "skipped"
fi

# --------------------------------------------------------------------------
#  8. BackPack
# --------------------------------------------------------------------------
step "Tunnel engine (BackPack)"
if [[ $WANT_BACKPACK -eq 0 ]]; then
  note "skipped by request"
elif [[ -x "$BACKPACK_BIN" ]]; then
  note "already installed at ${BACKPACK_BIN}"
else
  case "$(uname -m)" in
    aarch64|arm64) BP_ARCH="arm64" ;;
    *) BP_ARCH="amd64" ;;
  esac
  BP_URL="https://github.com/AminMGMT/BackPack/releases/latest/download/backpack_latest_linux_${BP_ARCH}"
  if download "$BP_URL" "${BACKPACK_BIN}.tmp"; then
    install -m 0755 "${BACKPACK_BIN}.tmp" "$BACKPACK_BIN"
    rm -f "${BACKPACK_BIN}.tmp"
    ok "installed ${BACKPACK_BIN}"
  else
    rm -f "${BACKPACK_BIN}.tmp"
    warn "could not download BackPack; tunnels will report the binary as missing"
    note "install it later, or place any compatible binary at ${BACKPACK_BIN}"
  fi
fi

# --------------------------------------------------------------------------
#  9. Host preparation for VPN traffic
# --------------------------------------------------------------------------
step "Preparing the host for VPN traffic"
spin "enable ip forwarding" bash -c '
  set -e
  install -d -m 0755 /etc/sysctl.d
  cat > /etc/sysctl.d/99-panah.conf <<SYSCTL
# Installed by Panah: required for routed VPN traffic.
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
SYSCTL
  sysctl --system >/dev/null 2>&1 || true
'
spin "prepare the tun device" bash -c '
  set -e
  modprobe tun 2>/dev/null || true
  [ -e /dev/net/tun ] || { mkdir -p /dev/net; mknod /dev/net/tun c 10 200; chmod 0600 /dev/net/tun; }
'

# --------------------------------------------------------------------------
#  10. systemd units
# --------------------------------------------------------------------------
if [[ "$MODE" == "panel" ]]; then
  step "Registering ${PANEL_SERVICE}"
  cat >"/etc/systemd/system/${PANEL_SERVICE}" <<EOF
[Unit]
Description=Panah VPN panel
Documentation=https://github.com/farnoudhosseini/vpn-panel
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=simple
# The panel manages OpenVPN, nginx and systemd units on this host, so it runs
# with the privileges those operations require.
User=root
WorkingDirectory=${SRC_DIR}
Environment=PYTHONPATH=${SRC_DIR}
EnvironmentFile=${ENV_FILE}
RuntimeDirectory=panah
RuntimeDirectoryMode=0750
ExecStart=${VENV_DIR}/bin/panah serve --host 127.0.0.1 --port ${INTERNAL_PORT}
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=3
TimeoutStopSec=20
KillSignal=SIGINT
LimitNOFILE=65535
# Contain a compromise without blocking the paths the panel manages.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
ProtectClock=yes
RestrictSUIDSGID=yes
ProtectSystem=full
ReadWritePaths=${DATA_DIR} ${CONFIG_DIR} ${LOG_DIR} /run/panah /etc/openvpn /etc/nginx /etc/backpack /etc/wireguard /etc/letsencrypt ${WEB_ROOT} /opt/panah

[Install]
WantedBy=multi-user.target
EOF
  spin "systemctl daemon-reload" systemctl daemon-reload
  spin "enable ${PANEL_SERVICE}" systemctl enable "$PANEL_SERVICE"
else
  step "Registering ${AGENT_SERVICE}"
  cat >"/etc/systemd/system/${AGENT_SERVICE}" <<EOF
[Unit]
Description=Panah node agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${SRC_DIR}
Environment=PYTHONPATH=${SRC_DIR}
EnvironmentFile=${ENV_FILE}
RuntimeDirectory=panah
RuntimeDirectoryMode=0750
ExecStart=${VENV_DIR}/bin/panah agent --host 0.0.0.0 --port ${AGENT_PORT}
Restart=always
RestartSec=3
LimitNOFILE=65535
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=full
ReadWritePaths=${DATA_DIR} ${CONFIG_DIR} ${LOG_DIR} /run/panah /etc/openvpn /etc/nginx /etc/backpack /etc/wireguard ${WEB_ROOT} /opt/panah

[Install]
WantedBy=multi-user.target
EOF
  spin "systemctl daemon-reload" systemctl daemon-reload
  spin "enable ${AGENT_SERVICE}" systemctl enable "$AGENT_SERVICE"
fi

# --------------------------------------------------------------------------
#  11. First-run provisioning
# --------------------------------------------------------------------------
ADMIN_USER="admin"
ADMIN_PASS=""
if [[ "$MODE" == "panel" ]]; then
  step "Provisioning the database and the administrator account"
  BOOTSTRAP_OUT="$(/usr/local/bin/panah bootstrap 2>>"$LOG_FILE")" || fail "bootstrap failed"
  # The CLI prints a labelled credential block only when it creates the account.
  ADMIN_USER="$(printf '%s\n' "$BOOTSTRAP_OUT" | sed -n 's/^username: //p' | head -1)"
  ADMIN_PASS="$(printf '%s\n' "$BOOTSTRAP_OUT" | sed -n 's/^password: //p' | head -1)"
  ADMIN_USER="${ADMIN_USER:-admin}"
  ok "schema ready · administrator '${ADMIN_USER}' exists"
fi

# --------------------------------------------------------------------------
#  12. nginx and TLS (panel)
# --------------------------------------------------------------------------
NGINX_HTTP2_LISTEN=""
if [[ "$MODE" == "panel" ]]; then
  step "Configuring nginx and TLS"

  # `listen ... http2` is the portable spelling: nginx < 1.25.1 only accepts
  # that form, and newer builds merely warn about it.
  NGINX_HTTP2_LISTEN=" http2"

  CERT_FULLCHAIN="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  CERT_PRIVKEY="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
  if [[ "$PUBLIC_PORT" == "443" ]]; then REDIRECT_TARGET="https://\$host"; else REDIRECT_TARGET="https://\$host:${PUBLIC_PORT}"; fi

  write_challenge_site() {
    cat >"/etc/nginx/sites-available/panah-panel.conf" <<EOF
# Managed by Panah install.sh — HTTP entrypoint used for the ACME challenge.
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root ${WEB_ROOT}/decoy;
        default_type "text/plain";
    }

    location / { return 301 ${REDIRECT_TARGET}\$request_uri; }
}
EOF
    ln -sf /etc/nginx/sites-available/panah-panel.conf /etc/nginx/sites-enabled/panah-panel.conf
    rm -f /etc/nginx/sites-enabled/default
  }

  write_tls_site() {
    cat >"/etc/nginx/sites-available/panah-panel.conf" <<EOF
# Managed by Panah install.sh — the panel's public entrypoint.
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root ${WEB_ROOT}/decoy;
        default_type "text/plain";
    }

    location / { return 301 ${REDIRECT_TARGET}\$request_uri; }
}

server {
    listen ${PUBLIC_PORT} ssl${NGINX_HTTP2_LISTEN};
    listen [::]:${PUBLIC_PORT} ssl${NGINX_HTTP2_LISTEN};
    server_name ${DOMAIN};

    ssl_certificate     ${CERT_FULLCHAIN};
    ssl_certificate_key ${CERT_PRIVKEY};
    ssl_session_timeout 1d;
    ssl_session_cache   shared:MozSSL:10m;
    ssl_session_tickets off;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;

    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    client_max_body_size 16m;
    client_body_timeout 60s;
    keepalive_timeout 65s;

    # The web interface, the API and subscription links share this origin.
    location / {
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
EOF
  }

  write_challenge_site
  spin "nginx configuration test" nginx -t
  spin "enable and start nginx" systemctl enable --now nginx
  spin "reload nginx" systemctl reload nginx

  if [[ $WANT_CERTBOT -eq 1 ]]; then
    if spin "request a Let's Encrypt certificate" \
        certbot certonly --webroot -w "${WEB_ROOT}/decoy" -d "$DOMAIN" \
          --non-interactive --agree-tos --keep-until-expiring -m "$EMAIL" --rsa-key-size 4096; then
      ok "certificate issued for ${DOMAIN}"
    else
      warn "certbot could not issue a certificate; continuing with a self-signed one"
    fi
  fi

  if [[ ! -s "$CERT_FULLCHAIN" ]]; then
    CERT_DIR="${CONFIG_DIR}/tls"
    spin "generate a self-signed certificate" bash -c "
      set -e
      install -d -m 0750 '${CERT_DIR}'
      openssl req -x509 -nodes -newkey rsa:3072 -days 825 \
        -keyout '${CERT_DIR}/server.key' -out '${CERT_DIR}/server.crt' \
        -subj '/CN=${DOMAIN}' -addext 'subjectAltName=DNS:${DOMAIN}'
      chmod 0600 '${CERT_DIR}/server.key'
    "
    CERT_FULLCHAIN="${CERT_DIR}/server.crt"
    CERT_PRIVKEY="${CERT_DIR}/server.key"
  fi

  spin "install the renewal hook" bash -c "
    set -e
    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    cat > /etc/letsencrypt/renewal-hooks/deploy/panah-reload.sh <<'HOOK'
#!/bin/sh
# Installed by Panah: reload the panel after a certificate renewal.
systemctl try-reload-or-restart ${PANEL_SERVICE} >/dev/null 2>&1 || true
HOOK
    chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/panah-reload.sh
  "

  write_tls_site
  spin "nginx configuration test" nginx -t
  spin "reload nginx" systemctl reload nginx
  ok "TLS termination ready on port ${PUBLIC_PORT}"
fi

# --------------------------------------------------------------------------
#  13. Start the service
# --------------------------------------------------------------------------
if [[ "$MODE" == "panel" ]]; then
  step "Starting ${PANEL_SERVICE}"
  spin "restart panel" systemctl restart "$PANEL_SERVICE"
  HEALTHY=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${INTERNAL_PORT}/health" >/dev/null 2>&1; then
      HEALTHY=1
      break
    fi
    sleep 1
  done
  if [[ $HEALTHY -eq 1 ]]; then
    ok "panel is answering on 127.0.0.1:${INTERNAL_PORT}"
  else
    warn "the panel did not answer /health within 30 seconds"
    note "inspect: journalctl -u ${PANEL_SERVICE} -n 60 --no-pager"
  fi
else
  step "Starting ${AGENT_SERVICE}"
  spin "restart agent" systemctl restart "$AGENT_SERVICE"
  if curl -fsS --max-time 3 "http://127.0.0.1:${AGENT_PORT}/agent/v1/health" >/dev/null 2>&1; then
    ok "agent is answering on 0.0.0.0:${AGENT_PORT}"
  else
    warn "the agent did not answer /agent/v1/health"
    note "inspect: journalctl -u ${AGENT_SERVICE} -n 60 --no-pager"
  fi
fi

# --------------------------------------------------------------------------
#  14. Firewall (opt-in)
# --------------------------------------------------------------------------
step "Firewall"
if [[ $WANT_FIREWALL -eq 1 ]] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  if [[ "$MODE" == "panel" ]]; then
    spin "allow ${PUBLIC_PORT}/tcp" ufw allow "${PUBLIC_PORT}/tcp"
    spin "allow 80/tcp" ufw allow 80/tcp
  else
    spin "allow ${AGENT_PORT}/tcp" ufw allow "${AGENT_PORT}/tcp"
  fi
  ok "ufw rules added"
else
  note "left untouched (pass --firewall to add rules when ufw is active)"
fi

# --------------------------------------------------------------------------
#  Summary
# --------------------------------------------------------------------------
hr
if [[ "$MODE" == "panel" ]]; then
  printf '%s  Panel installed%s\n\n' "${C_GREEN}${C_BOLD}" "$C_RESET"
  if [[ "$PUBLIC_PORT" == "443" ]]; then
    printf '   %sURL%s        https://%s\n' "$C_BOLD" "$C_RESET" "$DOMAIN"
  else
    printf '   %sURL%s        https://%s:%s\n' "$C_BOLD" "$C_RESET" "$DOMAIN" "$PUBLIC_PORT"
  fi
  printf '   %sUsername%s   %s\n' "$C_BOLD" "$C_RESET" "$ADMIN_USER"
  if [[ -n "$ADMIN_PASS" ]]; then
    printf '   %sPassword%s   %s %s(change it after signing in)%s\n' "$C_BOLD" "$C_RESET" "$ADMIN_PASS" "$C_DIM" "$C_RESET"
  else
    printf '   %sPassword%s   unchanged · panah reset-password %s\n' "$C_BOLD" "$C_RESET" "$ADMIN_USER"
  fi
  printf '   %sConfig%s     %s\n' "$C_BOLD" "$C_RESET" "$ENV_FILE"
  printf '   %sData%s       %s\n' "$C_BOLD" "$C_RESET" "$DATA_DIR"
  printf '   %sLogs%s       journalctl -u %s -f\n' "$C_BOLD" "$C_RESET" "$PANEL_SERVICE"
  printf '\n   %sInstall the agent on another server with:%s\n' "$C_BOLD" "$C_RESET"
  printf '   %s\n' "$NODE_INSTALL_CMD"
  printf '\n   %sAnyone holding the agent key can command your nodes — keep it secret.%s\n' "$C_DIM" "$C_RESET"
else
  NODE_IP="$(primary_ip)"
  printf '%s  Node agent installed%s\n\n' "${C_GREEN}${C_BOLD}" "$C_RESET"
  printf '   %sAgent%s      http://%s:%s/agent/v1\n' "$C_BOLD" "$C_RESET" "$NODE_IP" "$AGENT_PORT"
  printf '   %sConfig%s     %s\n' "$C_BOLD" "$C_RESET" "$ENV_FILE"
  printf '   %sLogs%s       journalctl -u %s -f\n' "$C_BOLD" "$C_RESET" "$AGENT_SERVICE"
  if [[ "${GENERATED_AGENT_KEY:-0}" == "1" ]]; then
    printf '\n   %sAgent key for this node (paste it into the panel):%s\n' "$C_BOLD" "$C_RESET"
    printf '   %s\n' "$AGENT_KEY"
  fi
  printf '\n   Add it in the panel: Nodes → Add node → address %s, port %s\n' "$NODE_IP" "$AGENT_PORT"
fi
printf '\n   %sInstall log%s  %s\n' "$C_DIM" "$C_RESET" "$LOG_FILE"
hr
printf '\n'
