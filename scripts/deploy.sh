#!/usr/bin/env bash
# deploy.sh — Deploy do Cortex no LXC 200 (10.11.12.200)
#
# Uso:
#   ./scripts/deploy.sh               # Deploy normal (main)
#   ./scripts/deploy.sh --branch dev  # Deploy de outra branch
#   ./scripts/deploy.sh --dry-run     # Só mostra o que faria
#
# O script:
#   1. Acessa o Proxmox host (10.11.12.46)
#   2. Entra no LXC 200 via pct exec
#   3. Faz git pull da branch main (ou --branch <branch>)
#   4. Rebuild e restart via docker compose
#   5. Aguarda health check em /health
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROXMOX_HOST="10.11.12.46"
PROXMOX_USER="root"
SSH_KEY="${HOME}/.ssh/id_ed25519"
LXC_ID="200"
APP_DIR="/opt/cortex"
BRANCH="main"
DRY_RUN=false
HEALTH_URL="http://10.11.12.200:8082/health"
HEALTH_RETRIES=12   # 12 × 5s = 60s timeout
HEALTH_WAIT=5

# ── Args ──────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      BRANCH="${2:?'--branch requer um valor'}"
      shift 2
      ;;
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

# Executa um script no LXC sem problemas de quoting:
#   1. Envia o script via stdin para um arquivo temp no Proxmox host
#   2. pct push copia o arquivo para dentro do LXC
#   3. pct exec roda o arquivo
#   4. Limpa os temporários
lxc_run_script() {
  local TMPHOST="/tmp/cortex_deploy_$$.sh"
  local TMPLXC="/tmp/cortex_deploy_$$.sh"

  # 1. Lê o heredoc do stdin e salva num arquivo local temporário
  local TMPLOCAL
  TMPLOCAL=$(mktemp)
  cat > "$TMPLOCAL"  # stdin (heredoc) → arquivo local

  # 2. Copia o arquivo local para o Proxmox host via scp
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
      "$TMPLOCAL" "${PROXMOX_USER}@${PROXMOX_HOST}:${TMPHOST}"
  rm -f "$TMPLOCAL"

  # 3. pct push copia do Proxmox host para dentro do LXC
  ssh_proxmox "pct push ${LXC_ID} ${TMPHOST} ${TMPLXC}"

  # 4. Executa dentro do LXC
  ssh_proxmox "pct exec ${LXC_ID} -- bash ${TMPLXC}"
  local rc=$?

  # 5. Limpa temporários
  ssh_proxmox "rm -f ${TMPHOST}; pct exec ${LXC_ID} -- rm -f ${TMPLXC}" 2>/dev/null || true
  return $rc
}

# ── Dry-run guard ─────────────────────────────────────────────────────────────
if $DRY_RUN; then
  warn "DRY-RUN ativado — nenhum comando será executado no LXC"
  log  "Proxmox : ${PROXMOX_HOST}"
  log  "LXC ID  : ${LXC_ID}"
  log  "App dir : ${APP_DIR}"
  log  "Branch  : ${BRANCH}"
  log  "Health  : ${HEALTH_URL}"
  exit 0
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

# ── Deploy ────────────────────────────────────────────────────────────────────
log "Entrando no LXC e fazendo deploy (branch: ${BRANCH})…"
lxc_run_script <<DEPLOY_SCRIPT
#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/rodrigoroldan/cortex-context.git"
APP_DIR="${APP_DIR}"
BRANCH="${BRANCH}"

echo "── Preparando diretório ──────────────────────────"
if [ ! -d "\${APP_DIR}/.git" ]; then
  echo "Diretório não encontrado ou sem .git — clonando repositório…"
  mkdir -p "\$(dirname "\${APP_DIR}")"
  git clone --branch "\${BRANCH}" "\${REPO_URL}" "\${APP_DIR}"
else
  echo "Repositório existente — fazendo pull…"
  cd "\${APP_DIR}"
  git fetch origin
  git checkout "\${BRANCH}"
  git pull origin "\${BRANCH}"
fi

cd "\${APP_DIR}"
echo "Commit atual: \$(git log --oneline -1)"

echo "── .env ──────────────────────────────────────────"
if [ ! -f ".env" ]; then
  echo "⚠️  .env não encontrado em \${APP_DIR}/.env — usando .env.example como base"
  cp .env.example .env
  echo "   Edite \${APP_DIR}/.env com as credenciais corretas antes do próximo deploy!"
else
  echo ".env presente ✅"
fi

echo "── Docker Compose rebuild + restart ──────────────"
docker compose pull --quiet 2>/dev/null || true
docker compose up -d --build --remove-orphans

echo "── Containers após deploy ────────────────────────"
docker compose ps
DEPLOY_SCRIPT

ok "Deploy concluído no LXC ${LXC_ID}"

# ── Health check ─────────────────────────────────────────────────────────────
log "Aguardando serviço ficar saudável em ${HEALTH_URL}…"
for i in $(seq 1 $HEALTH_RETRIES); do
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${HEALTH_URL}" 2>/dev/null || echo "000")
  if [[ "$HTTP_STATUS" == "200" ]]; then
    ok "Serviço respondendo (HTTP 200) — deploy OK!"
    exit 0
  fi
  log "Tentativa ${i}/${HEALTH_RETRIES} — HTTP ${HTTP_STATUS}, aguardando ${HEALTH_WAIT}s…"
  sleep $HEALTH_WAIT
done

err "Serviço não ficou saudável em $(( HEALTH_RETRIES * HEALTH_WAIT ))s. Verifique os logs:"
err "  ssh -i ${SSH_KEY} ${PROXMOX_USER}@${PROXMOX_HOST} 'pct exec ${LXC_ID} -- bash -c \"cd ${APP_DIR} && docker compose logs --tail=50\"'"
exit 1
