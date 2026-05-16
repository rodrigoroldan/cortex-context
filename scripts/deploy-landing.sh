#!/usr/bin/env bash
# deploy-landing.sh — Deploy da landing page do Cortex via Proxmox + LXC
#
# Uso:
#   ./scripts/deploy-landing.sh            # Deploy normal
#   ./scripts/deploy-landing.sh --dry-run  # Só mostra o que faria
#
# Configuração via variáveis de ambiente:
#   PROXMOX_HOST      — IP/hostname do Proxmox    (ex: 192.168.1.100)
#   LANDING_LXC_ID    — ID do LXC da landing page (ex: 214)
#   LANDING_PORT      — Porta do nginx da landing  (default: 8080)
#   SSH_KEY           — Caminho da chave SSH       (default: ~/.ssh/id_ed25519)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROXMOX_HOST="${PROXMOX_HOST:?'Defina PROXMOX_HOST (ex: export PROXMOX_HOST=192.168.1.100)'}"
PROXMOX_USER="${PROXMOX_USER:-root}"
SSH_KEY="${SSH_KEY:-${HOME}/.ssh/id_ed25519}"
LXC_ID="${LANDING_LXC_ID:?'Defina LANDING_LXC_ID (ex: export LANDING_LXC_ID=214)'}"
LANDING_SRC="$(cd "$(dirname "$0")/.." && pwd)/docs/landing/index.html"
# Diretório real que o nginx do LXC já serve
LANDING_DEST_DIR="/var/www/cortex-context"
LANDING_DEST="${LANDING_DEST_DIR}/index.html"
PORT="${LANDING_PORT:-8080}"
HEALTH_URL="${LANDING_HEALTH_URL:-http://localhost:${PORT}/}"
HEALTH_RETRIES=12
HEALTH_WAIT=3
DRY_RUN=false

# ── Args ──────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,2\}//'
      exit 0
      ;;
    *)
      echo "❌ Argumento desconhecido: $1" >&2
      exit 1
      ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { echo "$(date '+%H:%M:%S') ▶ $*"; }
ok()   { echo "$(date '+%H:%M:%S') ✅ $*"; }
warn() { echo "$(date '+%H:%M:%S') ⚠️  $*"; }
err()  { echo "$(date '+%H:%M:%S') ❌ $*" >&2; }

ssh_proxmox() {
  ssh -i "$SSH_KEY" \
      -o StrictHostKeyChecking=no \
      -o ConnectTimeout=10 \
      "${PROXMOX_USER}@${PROXMOX_HOST}" "$@"
}

# Copia um arquivo local → Proxmox host → dentro do LXC
push_file_to_lxc() {
  local LOCAL_SRC="$1"
  local LXC_DEST="$2"
  local TMPHOST="/tmp/landing_deploy_$$.html"

  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
      "$LOCAL_SRC" "${PROXMOX_USER}@${PROXMOX_HOST}:${TMPHOST}"
  ssh_proxmox "pct push ${LXC_ID} ${TMPHOST} ${LXC_DEST} && rm -f ${TMPHOST}"
}

# ── Dry-run guard ─────────────────────────────────────────────────────────────
if $DRY_RUN; then
  warn "DRY-RUN ativado — nenhum comando será executado no LXC"
  log  "Proxmox  : ${PROXMOX_HOST}"
  log  "LXC ID   : ${LXC_ID}"
  log  "Fonte    : ${LANDING_SRC}"
  log  "Destino  : ${LANDING_DEST}"
  log  "Porta    : ${PORT}"
  log  "Health   : ${HEALTH_URL}"
  exit 0
fi

# ── Verificação da fonte ──────────────────────────────────────────────────────
if [[ ! -f "$LANDING_SRC" ]]; then
  err "Arquivo de origem não encontrado: ${LANDING_SRC}"
  exit 1
fi

# ── Pre-flight ────────────────────────────────────────────────────────────────
log "Verificando acesso ao Proxmox…"
if ! ssh_proxmox "echo ok" &>/dev/null; then
  err "Não foi possível acessar ${PROXMOX_HOST}. Verifique a chave SSH e a conectividade."
  exit 1
fi
ok "Proxmox acessível"

log "Verificando LXC ${LXC_ID}…"
LXC_STATUS=$(ssh_proxmox "pct status ${LXC_ID} 2>&1")
if [[ "$LXC_STATUS" != *"running"* ]]; then
  err "LXC ${LXC_ID} não está em execução: ${LXC_STATUS}"
  exit 1
fi
ok "LXC ${LXC_ID} está running"

# ── Copia o arquivo ───────────────────────────────────────────────────────────
log "Copiando landing page para ${LANDING_DEST}…"
push_file_to_lxc "$LANDING_SRC" "$LANDING_DEST"
ok "index.html copiado para ${LANDING_DEST}"

# ── Reload nginx ──────────────────────────────────────────────────────────────
log "Recarregando nginx…"
ssh_proxmox "pct exec ${LXC_ID} -- systemctl reload nginx"
ok "nginx recarregado"

# ── Health check ─────────────────────────────────────────────────────────────
log "Aguardando landing responder em ${HEALTH_URL}…"
for i in $(seq 1 $HEALTH_RETRIES); do
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${HEALTH_URL}" 2>/dev/null || echo "000")
  if [[ "$HTTP_STATUS" == "200" ]]; then
    ok "Landing respondendo (HTTP 200) — deploy OK!"
    echo ""
    echo "  🌐 Landing disponível em: ${HEALTH_URL}"
    exit 0
  fi
  log "Tentativa ${i}/${HEALTH_RETRIES} — HTTP ${HTTP_STATUS}, aguardando ${HEALTH_WAIT}s…"
  sleep $HEALTH_WAIT
done

err "Landing não ficou acessível em $(( HEALTH_RETRIES * HEALTH_WAIT ))s."
err "Verifique os logs: ssh -i ${SSH_KEY} ${PROXMOX_USER}@${PROXMOX_HOST} 'pct exec ${LXC_ID} -- journalctl -u nginx --no-pager -n 30'"
exit 1
