# Dimension Schema — Cortex v2

Este documento descreve o schema YAML para definição de novas **dimension extensions** no Cortex.
Uma *dimension* é um tipo de nó do grafo com parser dedicado, campos estruturados e relacionamentos.

---

## Estrutura de um Dimension YAML

```yaml
# ── Metadados obrigatórios ──────────────────────────────────────────────────
dimension: <string>          # Identificador único da dimensão (ex: spec, service, workflow)
node_label: <string>         # Label do nó no Neo4j (ex: Spec, Service, TemporalWorkflow)
parser: <string>             # Chave do parser no ParserRegistry (ex: spec_md, agents_md)

# ── Fonte dos dados ──────────────────────────────────────────────────────────
source_pattern: <glob>       # Glob único (alternativa a source_patterns)
source_patterns:             # Lista de globs (quando múltiplos padrões são necessários)
  - "**/*.md"

# ── Campos extraídos ─────────────────────────────────────────────────────────
fields:
  - name: <string>           # Nome do campo (como propriedade no Neo4j)
    type: <string>           # string | integer | float | boolean | list[string]
    description: <string>
    required: <bool>         # default: false
    derivation: <string>     # (opcional) como o campo é calculado

# ── Relacionamentos produzidos ───────────────────────────────────────────────
relationships:
  - type: <string>           # ex: AFFECTS, DEPENDS_ON, IMPLEMENTS
    direction: outbound|inbound
    target_label: <string>   # Label do nó destino (ex: Service)
    source_label: <string>   # Label do nó origem (usado quando direction=inbound)
    description: <string>
    built_by: parser|relationship_builder  # quem cria essa aresta

# ── Índices Neo4j ────────────────────────────────────────────────────────────
indexes:
  - type: constraint_unique  # constraint UNIQUE em uma propriedade
    property: <string>
    cypher: <string>         # Cypher exato para criar o constraint
  - type: fulltext           # índice full-text para FTS
    name: <string>
    properties: list[string]
    cypher: <string>         # Cypher exato para criar o índice
```

---

## Dimensões Ativas

### `spec` — Especificações do produto

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | `spec-NNN` — identificador único |
| `number` | integer | Número sequencial |
| `title` | string | Título do H1 do plan.md |
| `status` | string | `completed` \| `in-progress` \| `planned` \| `todo` |
| `labels` | list[string] | Labels inferidos do slug + conteúdo |
| `summary` | string | Primeira frase significativa |
| `repos` | list[string] | Repos mencionados no texto |
| `file_path` | string | Caminho relativo ao arquivo fonte |
| `labels_str` | string | Labels concatenados (usado no FTS) |

**Parser**: `spec_md` → `SpecParser`  
**Fonte**: `specs/**/plan.md` (em role-organizado-workspace)  
**Relacionamentos outbound**: `SUPERSEDES`, `EVOLVES_FROM`, `DEPENDS_ON`, `RELATED_TO`, `IMPLEMENTS`, `AFFECTS`

---

### `service` — Serviços do ecossistema

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | `service-{repo-short}` — ex: `service-backend` |
| `name` | string | Nome legível do H1 do agents.md |
| `repo` | string | Nome completo do repositório |
| `tech` | list[string] | Stack tecnológica (tabela Stack) |
| `capabilities` | list[string] | Capacidades declaradas |
| `port` | integer | Porta principal (se mencionada) |
| `url` | string | URL pública (rolds.dev domain grep) |
| `version` | string | Versão do agents.md |
| `capabilities_str` | string | Capabilities concatenadas (usado no FTS) |

**Parser**: `agents_md` → `AgentsMdParser`  
**Fonte**: `**/AGENTS.md`, `**/agents.md` (em cada repo listado em `cortex.config.yaml`)  
**Relacionamentos inbound**: `AFFECTS` (de `:Spec` nodes via `Spec.repos[]`)

---

## Como Adicionar uma Nova Dimensão

1. **Criar o YAML** em `app/dimensions/{nome}.yaml` com o schema acima
2. **Criar o Parser** em `app/core/parsers/{nome}_parser.py` implementando `DimensionParser`:
   ```python
   from app.core.parsers.base import DimensionParser, NodeData

   class MeuParser(DimensionParser):
       def can_parse(self, file_path: Path, config: DimensionConfig) -> bool:
           return file_path.name.endswith("minha-extensao.md")

       def parse(self, file_path: Path, config: DimensionConfig) -> list[NodeData]:
           content = file_path.read_text()
           # ... extração de campos ...
           return [NodeData(node_label="MeuNo", properties={...})]
   ```
3. **Registrar o Parser** em `app/core/parser_registry.py`:
   ```python
   REGISTRY = {
       "spec_md": SpecParser(),
       "agents_md": AgentsMdParser(),
       "meu_parser": MeuParser(),  # ← adicionar aqui
   }
   ```
4. **Ativar a Dimensão** em `cortex.config.yaml`:
   ```yaml
   active_dimensions:
     - spec
     - service
     - minha-nova-dimensao  # ← adicionar aqui
   ```
5. **Implementar índices** — o `dimension_loader` executa os Cyphers declarados no YAML

---

## Roadmap de Dimensões

| Dimensão | Spec | Status |
|----------|------|--------|
| `spec` | spec-144 | ✅ ativo |
| `service` | spec-145 | 🚧 em implementação |
| `temporal-workflow` | spec-146 | 📋 planejado |
| `decision-adr` | spec-148+ | 📋 planejado |
