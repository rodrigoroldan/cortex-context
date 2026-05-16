# ZFS Snapshot & Recovery — Cortex LXC

> **Ambiente**: Proxmox LXC rodando o Cortex Knowledge Graph  
> **Última atualização**: 2026

> **Adapte ao seu ambiente**: substitua `<PROXMOX_HOST>` pelo IP/hostname do seu Proxmox,
> e `<LXC_ID>` pelo ID do seu container Cortex.

## Visão Geral

O Neo4j do Cortex persiste dados em `/var/lib/docker/volumes/cortex_neo4j-data/`.
Snapshots ZFS permitem backup atômico do volume sem parar o container.

O pool ZFS no Proxmox é `rpool` ou `local-zfs` (verificar com `zpool list` no host Proxmox).
O LXC fica em `local-zfs/subvol-<LXC_ID>-disk-0` (ou similar — confirmar abaixo).

---

## Pré-requisitos

Todos os comandos rodam no **host Proxmox**, não dentro do LXC.

```bash
ssh root@<PROXMOX_HOST>
```

Confirmar o dataset ZFS do LXC:

```bash
zfs list | grep <LXC_ID>
# Exemplo de saída esperada:
# rpool/data/subvol-<LXC_ID>-disk-0    12G  ...
```

---

## Criar Snapshot (antes de deploy ou manutenção)

```bash
# No host Proxmox:
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
DATASET="rpool/data/subvol-<LXC_ID>-disk-0"   # ajustar se diferente

# Criar snapshot atômico (sem parar o LXC):
zfs snapshot "${DATASET}@cortex-${TIMESTAMP}"

# Verificar:
zfs list -t snapshot | grep <LXC_ID>
```

> **Nota**: O Neo4j usa journaling interno. O snapshot ZFS é consistente a nível de bloco,
> mas para consistência máxima do banco, pause as escritas antes (opcional):
>
> ```bash
> # Dentro do LXC — pausar cortex temporariamente:
> docker pause cortex-api   # pausa sem matar conexões
> zfs snapshot "${DATASET}@cortex-${TIMESTAMP}-consistent"
> docker unpause cortex-api
> ```

---

## Listar Snapshots

```bash
# No host Proxmox:
zfs list -t snapshot -o name,creation,used | grep <LXC_ID>
```

---

## Restaurar Snapshot (rollback)

> ⚠️ **DESTRUTIVO**: rollback apaga todos os dados escritos após o snapshot.
> Confirme o snapshot alvo antes de executar.

```bash
# No host Proxmox:

# 1. Parar o LXC:
pct stop <LXC_ID>

# 2. Rollback para o snapshot:
SNAPSHOT="rpool/data/subvol-<LXC_ID>-disk-0@cortex-20260101-120000"  # ajustar
zfs rollback "${SNAPSHOT}"

# 3. Reiniciar o LXC:
pct start <LXC_ID>

# 4. Verificar Neo4j e Cortex API:
# (aguardar ~30s para o Neo4j inicializar)
curl -s http://localhost:8082/health | python3 -m json.tool
```

---

## Remover Snapshots Antigos

```bash
# Listar e remover snapshots com mais de 30 dias:
zfs list -t snapshot -o name,creation | grep "subvol-<LXC_ID>-disk-0@cortex" | \
  awk 'NR>1 {print $1}' | \
  while read snap; do
    echo "Removendo: $snap"
    zfs destroy "$snap"
  done
```

Para manter apenas os últimos N snapshots:

```bash
# Manter apenas os últimos 5 snapshots do LXC:
zfs list -t snapshot -H -o name | grep "subvol-<LXC_ID>-disk-0@cortex" | \
  sort | head -n -5 | xargs -I{} zfs destroy {}
```

---

## Rotina Recomendada

| Quando                        | Ação                                              |
| ----------------------------- | ------------------------------------------------- |
| Antes de deploy Cortex        | `zfs snapshot ... @cortex-pre-deploy-<timestamp>` |
| Antes de re-ingestão completa | `zfs snapshot ... @cortex-pre-ingest-<timestamp>` |
| Semanalmente (CI/CD)          | Snapshot automático via cron no Proxmox           |
| Após 30 dias                  | Remover snapshots antigos                         |

---

## Automação via Cron (Proxmox host)

```bash
# /etc/cron.d/cortex-snapshots no host Proxmox:
# Snapshot semanal às 02:00 domingo
0 2 * * 0 root /usr/local/bin/cortex-snapshot.sh

# /usr/local/bin/cortex-snapshot.sh
#!/bin/bash
DATASET="rpool/data/subvol-200-disk-0"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
zfs snapshot "${DATASET}@cortex-weekly-${TIMESTAMP}"
# Manter apenas últimos 4 snapshots semanais:
zfs list -t snapshot -H -o name | grep "subvol-200-disk-0@cortex-weekly" | \
  sort | head -n -4 | xargs -I{} zfs destroy {}
logger "Cortex ZFS snapshot criado: cortex-weekly-${TIMESTAMP}"
```

---

## Troubleshooting

| Problema                                | Solução                                                            |
| --------------------------------------- | ------------------------------------------------------------------ |
| `dataset does not exist`                | Verificar nome exato com `zfs list \| grep 200`                    |
| `cannot rollback: dataset has children` | Usar `zfs rollback -r` (remove snapshots filhos)                   |
| Neo4j não inicia após rollback          | Verificar `docker logs cortex-neo4j`; aguardar recovery do journal |
| Snapshot falha com I/O busy             | Pausar container antes: `docker pause cortex-neo4j cortex-api`     |

---

## Referências

- [Proxmox ZFS docs](https://pve.proxmox.com/wiki/ZFS_on_Linux)
- [Neo4j backup best practices](https://neo4j.com/docs/operations-manual/current/backup-restore/)
- LXC 200 info: `pct config 200` no host Proxmox
