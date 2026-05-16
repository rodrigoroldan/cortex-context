# Dimension Schema — Cortex v3 (I.S.I.R)

Este documento descreve o schema YAML para definição de **dimensões** no Cortex v3.
Uma *dimensão* mapeia um tipo de nó do grafo com um parser, fonte de dados, campos estruturados e relacionamentos.

---

## Ontologia I.S.I.R

Todos os nós do grafo pertencem a um dos 4 pilares universais:

| Pilar | O que representa | Exemplos de node_label |
|-------|-----------------|----------------------|
| `Intent` | O "Por quê" — requisitos, intenções | `Spec`, `Epic`, `Story` |
| `System` | O "Como" alto nível — arquitetura | `Service`, `ADR`, `Database` |
| `Implementation` | O "O quê" concreto | `Workflow`, `Module`, `Component` |
| `Runtime` | O "Mundo real" — observabilidade | `Alert`, `Deployment`, `Incident` |

No Neo4j cada nó recebe **multi-labels**: `[node_label, pillar]`
```cypher
-- Exemplos
MERGE (n:Spec) SET n:Intent              -- (:Spec:Intent)
MERGE (n:Service) SET n:System           -- (:Service:System)
MERGE (n:Workflow) SET n:Implementation
MERGE (n:ADR) SET n:System
MERGE (n:DocumentChunk) SET n:Intent     -- chunk de uma Spec para Vector RAG
```

---

## Schema de um Dimension YAML

```yaml
# ── Metadados obrigatórios ──────────────────────────────────────────────────
dimension: <string>      # Chave única da dimensão (ex: spec, service, adr)
node_label: <string>     # Label do nó no Neo4j (ex: Spec, Service, Workflow)
pillar: <string>         # Um dos 4 pilares: Intent | System | Implementation | Runtime
parser: <string>         # Chave do parser: builtin.* ou chave de plugin customizado

# ── Fonte dos dados ──────────────────────────────────────────────────────────
source_type: <string>    # filesystem | github_api | url | plugin
source_path: <string>    # Caminho/URL raiz (ex: /workspace, /specs)
source_pattern: <glob>   # Glob único (alternativa a source_patterns)
source_patterns:         # Lista de globs para múltiplos padrões
  - "**/*.md"
  - "**/plan.md"

# ── Campos extraídos ─────────────────────────────────────────────────────────
fields:
  - name: <string>        # Nome do campo (propriedade no Neo4j)
    type: <string>        # string | integer | float | boolean | list[string]
    description: <string>
    required: <bool>      # default: false
    derivation: <string>  # (opcional) descrição de como o campo é calculado

# ── Relacionamentos produzidos ───────────────────────────────────────────────
relationships:
  - type: <string>               # Edge canônica (ver seção CANONICAL_EDGES)
    direction: outbound|inbound
    target_label: <string>       # Label do nó destino
    source_label: <string>       # Label do nó origem (quando direction=inbound)
    description: <string>
    built_by: parser|relationship_builder

# ── Índices Neo4j ────────────────────────────────────────────────────────────
indexes:
  - type: constraint_unique
    property: <string>
    cypher: <string>             # Cypher exato do constraint
  - type: fulltext
    name: <string>
    properties: list[string]
    cypher: <string>             # Cypher exato do índice
```

---

## Arestas Canônicas (CANONICAL_EDGES)

```
DEPENDS_ON          Intent ↔ Intent          — Spec A requer Spec B
SUPERSEDES          Intent/System ↔ mesma    — Spec/ADR A substitui B
EVOLVES_FROM        Intent ↔ Intent          — Spec A é evolução de Spec B
RELATED_TO          Qualquer ↔ Qualquer      — Referência cruzada genérica
AFFECTS             Intent/System → System   — Spec/ADR afeta Serviço
IMPLEMENTS          Implementation → Intent  — Workflow implementa uma Spec
MOTIVATED_BY        System → Intent          — ADR motivado por uma Spec
IMPLEMENTS_DECISION Implementation → System  — Workflow implementa uma decisão ADR
EXPOSES             System → System          — Serviço expõe uma API/Endpoint
CALLS               Implementation ↔         — Módulo/Workflow chama outro
TRIGGERS            Implementation ↔         — Workflow A dispara Workflow B
MUTATES             Implementation →         — Operação muta entidade de domínio
OBSERVED_IN         Runtime → System         — Alerta observado em Serviço
DEPLOYED_TO         Implementation → System  — Workflow/Artefato em Serviço
CHUNK_OF            DocumentChunk → *        — Chunk pertence ao nó pai (Vector RAG)
```

---

## Parsers Builtin

### `builtin.markdown_frontmatter`

Parser genérico para qualquer arquivo Markdown com ou sem frontmatter YAML.

**Extrai automaticamente**:
- Frontmatter YAML (bloco `---`) — título, status, date, summary, labels
- H1 como `title` (fallback quando não há frontmatter)
- Primeira frase significativa como `summary`
- Referências cruzadas textuais → arestas `DEPENDS_ON`, `SUPERSEDES`, `RELATED_TO`, `EVOLVES_FROM`
- `labels` inferidos do slug do diretório + conteúdo
- `DocumentChunk` nodes para Vector RAG (quando `CORTEX_EMBEDDING_PROVIDER != none`)

**Compatível com**: MADR, ADRs, specs no padrão `NNN-slug/plan.md`, qualquer doc Markdown

**Usado por**: `spec`, `adr`, `workflow`

---

### `builtin.agents_manifest`

Parser para manifestos de serviço (`AGENTS.md`, `agents.md`, `README.md`).

**Extrai automaticamente**:
- Nome do serviço (H1)
- Stack tecnológico (Java, Python, Node.js, MongoDB, Redis, Docker, AWS, etc.)
- Capacidades da seção "Capabilities / Capacidades / Features"
- Porta principal (grep por `:PORT`, `port PORT`, `porta PORT`)
- URL pública

**Usado por**: `service`

---

## Dimensões Ativas

### `spec` — Especificações de produto
**Pilar**: `Intent` | **Parser**: `builtin.markdown_frontmatter`
**Fonte**: `specs/**/plan.md` (filesystem `/specs`)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | `spec-NNN` — identificador único |
| `number` | integer | Número sequencial |
| `title` | string | H1 do plan.md |
| `status` | string | `completed` \| `in-progress` \| `planned` \| `todo` |
| `labels` | list[string] | Labels inferidos do slug + conteúdo |
| `summary` | string | Primeira frase significativa |
| `repos` | list[string] | Repos mencionados no texto |
| `file_path` | string | Caminho relativo ao arquivo fonte |
| `labels_str` | string | Labels concatenados (FTS) |

**Relacionamentos outbound**: `SUPERSEDES`, `EVOLVES_FROM`, `DEPENDS_ON`, `RELATED_TO`, `IMPLEMENTS`, `AFFECTS`

---

### `service` — Serviços do ecossistema
**Pilar**: `System` | **Parser**: `builtin.agents_manifest`
**Fonte**: `AGENTS.md` / `agents.md` via `github_api` (ou filesystem como fallback)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | `service-{repo-short}` (ex: `service-backend`) |
| `name` | string | Nome legível do H1 do manifesto |
| `repo` | string | Nome completo do repositório |
| `tech` | list[string] | Stack tecnológica detectada |
| `capabilities` | list[string] | Capacidades declaradas |
| `port` | integer | Porta principal (se mencionada) |
| `url` | string | URL pública (grep por domínio) |
| `version` | string | Versão do manifesto |
| `capabilities_str` | string | Capabilities concatenadas (FTS) |

**Relacionamentos inbound**: `AFFECTS` (de `:Spec` via `Spec.repos[]`)

---

### `workflow` — Workflows e orquestração
**Pilar**: `Implementation` | **Parser**: `builtin.markdown_frontmatter`
**Fonte**: `**/temporal/**/*.yaml`, `**/workflows/**/*.md`

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | `workflow-{slug}` |
| `name` | string | Nome legível do workflow |
| `schedule` | string | Expressão cron ou descrição do agendamento |
| `mode` | string | `DISABLED` \| `SHADOW` \| `PRIMARY` |
| `service` | string | Serviço que executa o workflow |

**Relacionamentos**: `IMPLEMENTS` → Spec, `DEPLOYED_TO` → Service, `TRIGGERS` → Workflow

---

### `adr` — Architecture Decision Records
**Pilar**: `System` | **Parser**: `builtin.markdown_frontmatter`
**Fonte**: `**/docs/arquitetura/**/*.md`, `**/docs/adr/**/*.md`, `**/docs/architecture/**/*.md`, `**/docs/decisions/**/*.md`

Compatível com MADR e qualquer `.md` com frontmatter `title/status/date`.

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | `adr-{slug}` (derivado do caminho do arquivo) |
| `title` | string | H1 ou `frontmatter.title` |
| `status` | string | `proposed` \| `accepted` \| `deprecated` \| `superseded` \| `rejected` |
| `summary` | string | Primeiro parágrafo ou `frontmatter.summary` |
| `date` | string | Data da decisão (`YYYY-MM-DD`) |
| `labels` | list[string] | Tags semânticas inferidas do slug + frontmatter |

**Relacionamentos**: `SUPERSEDES` → ADR, `RELATED_TO` → ADR, `AFFECTS` → Service, `MOTIVATED_BY` → Spec, `IMPLEMENTS_DECISION` → Workflow

---

## Como Adicionar uma Nova Dimensão

### Opção A — Usar parser builtin (sem código)

1. **Criar o YAML** em `app/dimensions/{nome}.yaml`:
   ```yaml
   dimension: minha-dimensao
   node_label: MinhaEntidade
   pillar: Intent          # ou System, Implementation, Runtime
   parser: builtin.markdown_frontmatter
   source_type: filesystem
   source_path: "/workspace"
   source_patterns:
     - "**/docs/minha-pasta/**/*.md"
   fields:
     - name: id
       type: string
       required: true
   indexes:
     - type: constraint_unique
       property: id
       cypher: "CREATE CONSTRAINT minha_entidade_id IF NOT EXISTS FOR (n:MinhaEntidade) REQUIRE n.id IS UNIQUE"
   ```

2. **Ativar em `cortex.config.yaml`**:
   ```yaml
   active_dimensions:
     - minha-dimensao
   ```

### Opção B — Plugin customizado

1. **Criar o plugin** em `plugins/meu_parser.py`:
   ```python
   from app.core.parsers.base import BaseCortexExtractor, NodeData, ParseResult

   class MeuParser(BaseCortexExtractor):
       extractor_key = "meu.parser"   # referenciado no dimension YAML

       def can_parse(self, file_path):
           return file_path.suffix == ".json"

       def parse(self, file_path, dimension_config):
           content = file_path.read_text()
           # ... extração ...
           return ParseResult(nodes=[
               NodeData(
                   node_labels=[dimension_config.node_label, dimension_config.pillar],
                   node_id="minha-entidade-001",
                   properties={"id": "minha-entidade-001", "title": "..."}
               )
           ])
   ```

2. **Criar o YAML** com `parser: meu.parser`

3. **Auto-descoberta automática** via `discover_plugins()` — sem registro manual.

---

## Vector RAG — DocumentChunk

Quando `CORTEX_EMBEDDING_PROVIDER != none`, o `builtin.markdown_frontmatter` fragmenta documentos em nós `DocumentChunk` usando `chunker.py`:

- `chunk_markdown(content, chunk_size=600, overlap=100)` — divide por headings → parágrafos → sentenças
- Cada chunk gera `(:DocumentChunk:{pillar})` com embedding `float[]` no Neo4j
- Aresta `CHUNK_OF` conecta cada chunk ao nó pai (ex: `:Spec`, `:ADR`)
- Índice vetorial ANN criado via `apply_vector_index()` em `db/neo4j.py`
- O endpoint `POST /api/v1/query/semantic` executa **Hybrid GraphRAG**: ANN → resolve pais → expansão 1-hop

Configure em `cortex.config.yaml`:
```yaml
vector:
  provider: "${CORTEX_EMBEDDING_PROVIDER:none}"  # none | local | openai
  chunk_size: 600
  chunk_overlap: 100
```

---

## Roadmap de Dimensões

| Dimensão | Pilar | Status |
|----------|-------|--------|
| `spec` | Intent | ✅ ativo |
| `service` | System | ✅ ativo |
| `workflow` | Implementation | ✅ ativo |
| `adr` | System | ✅ ativo |
| `alert` | Runtime | 📋 planejado |
| `deployment` | Runtime | 📋 planejado |
