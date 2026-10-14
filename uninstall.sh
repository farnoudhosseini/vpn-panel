#!/usr/bin/env bash
# ==========================================================================
#  Panah VPN Panel — uninstaller
#  ------------------------------------------------------------------------
#  Usage:
#     curl -fsSL https://raw.githubusercontent.com/farnoudhosseini/vpn-panel/main/uninstall.sh | sudo bash
#     sudo ./uninstall.sh --purge -y          # also delete data, PKI and secrets
#     sudo ./uninstall.sh --mode node -y      # remove an agent install
#
#  By default the database, PKI and generated client bundles are kept so an
#  accidental removal can be reversed. Only --purge deletes them.
# ==========================================================================
set -Eeuo pipefail

PRODUCT="Panah"
SRC_DIR="/opt/panah/src"
VENV_DIR="/opt/panah/venv"
DATA_DIR="/opt/panah/data"
ROOT_DIR="/opt/panah"
CONFIG_DIR="/etc/panah"
ENV_FILE="${CONFIG_DIR}/panel.env"
LOG_DIR="/var/log/panah"
WEB_ROOT="/var/www/panah"
BACKPACK_BIN="/usr/local/bin/backpack"
BACKPACK_DIR="/etc/backpack"
OPENVPN_DIR="/etc/openvpn/panah"
PANEL_SERVICE="panah.service"
AGENT_SERVICE="panah-agent.service"
SYSCTL_FILE="/etc/sysctl.d/99-panah.conf"
NGINX_SITE="/etc/nginx/sites-available/panah-panel.conf"

MODE="auto"
PURGE=0
ASSUME_YES=0
TTY=0
[[ -t 1 ]] && TTY=1

if [[ $TTY -eq 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_RED=$'\033[38;5;203m'; C_GREEN=$'\033[38;5;78m'; C_AMBER=$'\033[38;5;215m'
else
  C_RESET=""; C_DIM=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_AMBER=""
fi

step() { printf '\n%s▸ %s%s\n' "$C_RED" "$*" "$C_RESET"; }
ok()   { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
note() { printf '  %s·%s %s%s%s\n' "$C_AMBER" "$C_RESET" "$C_DIM" "$*" "$C_RESET"; }
fail() { printf '\n  %s✘ %s%s\n' "$C_RED" "$*" "$C_RESET"; exit 1; }

on_error() {
  local code=$?
  printf '\n  %s✘ failed at line %s (exit %s)%s\n' "$C_RED" "${BASH_LINENO[0]:-?}" "$code" "$C_RESET"
  exit "$code"
}
trap on_error ERR

usage() {
  cat <<EOF
${PRODUCT} uninstaller

  --mode panel|node|auto   which installation to remove (default: auto detect)
  --purge                  also delete the database, PKI, secrets and logs
  --keep-backpack          leave ${BACKPACK_BIN} in place
  -y, --yes                do not ask for confirmation
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    --keep-backpack) KEEP_BACKPACK=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n\n' "$1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  printf '%sThis uninstaller must run as root (try: sudo bash uninstall.sh)%s\n' "$C_RED" "$C_RESET" >&2
  exit 1
fi

if [[ "$MODE" == "auto" ]]; then
  if [[ -r "$ENV_FILE" ]]; then
    MODE="$(sed -n 's/^PANAH_MODE=//p' "$ENV_FILE" | head -1)"
  fi
  if [[ "$MODE" != "panel" && "$MODE" != "node" ]]; then
    if [[ -f "/etc/systemd/system/${PANEL_SERVICE}" || -f "/etc/systemd/system/${AGENT_SERVICE}" ]]; then
      [[ -f "/etc/systemd/system/${PANEL_SERVICE}" ]] && MODE="panel" || MODE="node"
    else
      MODE="panel"
    fi
  fi
fi
[[ "$MODE" == "panel" || "$MODE" == "node" ]] || fail "--mode must be 'panel', 'node' or 'auto'"

printf '\n%s  %s uninstaller%s\n' "$C_BOLD" "$PRODUCT" "$C_RESET"
printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────────${C_RESET}"
printf '  mode: %s%s%s\n' "$C_BOLD" "$MODE" "$C_RESET"

# What disappears, and what survives.
REMOVE_LIST=(
  "/etc/systemd/system/${PANEL_SERVICE}"
  "/etc/systemd/system/${AGENT_SERVICE}"
  "/usr/local/bin/panah"
  "$VENV_DIR"
  "$SRC_DIR"
  "$CONFIG_DIR"
  "$LOG_DIR"
)
if [[ "$MODE" == "panel" ]]; then
  REMOVE_LIST+=("$NGINX_SITE" "/etc/nginx/sites-enabled/panah-panel.conf")
fi
if [[ "$PURGE" -eq 1 ]]; then
  REMOVE_LIST+=("$DATA_DIR" "$OPENVPN_DIR" "$BACKPACK_DIR" "$WEB_ROOT" "$SYSCTL_FILE")
fi

printf '\n  %sWill be removed:%s\n' "$C_BOLD" "$C_RESET"
for item in "${REMOVE_LIST[@]}"; do
  [[ -e "$item" || -L "$item" ]] && printf '    %s\n' "$item"
done
if [[ "$PURGE" -eq 0 ]]; then
  printf '\n  %sKept (use --purge to delete):%s\n' "$C_BOLD" "$C_RESET"
  for item in "$DATA_DIR" "$OPENVPN_DIR" "$BACKPACK_DIR" "$WEB_ROOT"; do
    [[ -e "$item" ]] && printf '    %s\n' "$item"
  done
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  if [[ ! -r /dev/tty ]]; then fail "no terminal to confirm on; pass -y"; fi
  printf '\n  %sContinue? [y/N]%s ' "$C_BOLD" "$C_RESET"
  read -r answer </dev/tty
  [[ "$answer" =~ ^[Yy]$ ]] || { printf '  aborted\n'; exit 0; }
fi

# --------------------------------------------------------------------------
#  Stop services
# --------------------------------------------------------------------------
step "Stopping services"
for unit in "$PANEL_SERVICE" "$AGENT_SERVICE"; do
  if systemctl list-unit-files "$unit" >/dev/null 2>&1 && [[ -f "/etc/systemd/system/${unit}" ]]; then
    systemctl stop "$unit" >/dev/null 2>&1 || true
    systemctl disable "$unit" >/dev/null 2>&1 || true
    ok "${unit} stopped and disabled"
  fi
done

# OpenVPN instances the panel created for inbounds.
if [[ -d "$OPENVPN_DIR" ]]; then
  for conf in "$OPENVPN_DIR"/*.conf; do
    [[ -e "$conf" ]] || continue
    unit="openvpn-server@$(basename "${conf%.conf}").service"
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    unit="openvpn-panah-$(basename "${conf%.conf}").service"
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
  done
  ok "OpenVPN instances stopped"
fi

# Tunnels and wireguard interfaces.
if command -v wg-quick >/dev/null 2>&1 && [[ -d /etc/wireguard ]]; then
  for iface in /etc/wireguard/panah-*.conf; do
    [[ -e "$iface" ]] || continue
    wg-quick down "$(basename "${iface%.conf}")" >/dev/null 2>&1 || true
  done
  ok "wireguard interfaces down"
fi
for unit in /etc/systemd/system/panah-tunnel-*.service /etc/systemd/system/backpack-*.service; do
  [[ -e "$unit" ]] || continue
  systemctl disable --now "$(basename "$unit")" >/dev/null 2>&1 || true
  rm -f "$unit"
done

# --------------------------------------------------------------------------
#  nginx
# --------------------------------------------------------------------------
if [[ "$MODE" == "panel" ]] && command -v nginx >/dev/null 2>&1; then
  step "Removing the nginx site"
  rm -f "$NGINX_SITE" /etc/nginx/sites-enabled/panah-panel.conf
  # Decoy and stream sites the panel installed for inbounds.
  rm -f /etc/nginx/sites-enabled/panah-*.conf /etc/nginx/sites-available/panah-*.conf
  rm -f /etc/nginx/conf.d/panah-stream*.conf
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx >/dev/null 2>&1 || true
    ok "nginx reloaded"
  else
    note "nginx configuration did not validate after removal; check /etc/nginx manually"
  fi
fi

# --------------------------------------------------------------------------
#  Files
# --------------------------------------------------------------------------
step "Removing Panah files"
for item in "${REMOVE_LIST[@]}"; do
  if [[ -L "$item" || -f "$item" ]]; then
    rm -f "$item"
    ok "removed ${item}"
  elif [[ -d "$item" ]]; then
    rm -rf "$item"
    ok "removed ${item}"
  fi
done

if [[ "${KEEP_BACKPACK:-0}" -eq 0 && "$PURGE" -eq 1 && -x "$BACKPACK_BIN" ]]; then
  rm -f "$BACKPACK_BIN"
  ok "removed ${BACKPACK_BIN}"
fi

systemctl daemon-reload >/dev/null 2>&1 || true
ok "systemd reloaded"

# --------------------------------------------------------------------------
#  Notes
# --------------------------------------------------------------------------
printf '\n'
printf '%s  %s removed%s\n\n' "${C_GREEN}${C_BOLD}" "$PRODUCT" "$C_RESET"
if [[ "$PURGE" -eq 0 ]]; then
  printf '  %sKept:%s %s (database, PKI, generated client configs)\n' "$C_BOLD" "$C_RESET" "$DATA_DIR"
  printf '  %sTo delete everything as well:%s uninstall.sh --purge\n' "$C_DIM" "$C_RESET"
fi
printf '  %sLeft in place on purpose:%s packages (openvpn, nginx, certbot), system users and\n' "$C_DIM" "$C_RESET"
printf '  Let'"'"'s Encrypt certificates under /etc/letsencrypt.\n'
printf '\n'
