# Integacao Chatwoot <-> Supabase -- Plano de Implementacao

> **Data**: 2026-04-10 | **Branch**: `dev` | **Risco para Supabase**: ZERO

---

## Checklist de Tarefas

- [ ] **FASE 0** -- Criar labels e custom attributes no Chatwoot via API
- [ ] **FASE 1** -- Adicionar vars Chatwoot ao `config.py` (+4 linhas)
- [ ] **FASE 2** -- Criar modulo `agente_2w/chatwoot_sync.py` (~130 linhas)
- [ ] **FASE 3** -- Import + signature em `_nucleo.py` (+2 params)
- [ ] **FASE 4a** -- Hook A: custom attributes do cliente (apos L800)
- [ ] **FASE 4b** -- Hook B: nome do cliente (apos L853)
- [ ] **FASE 4c** -- Hook C: label de etapa (apos L975)
- [ ] **FASE 4d** -- Hook D1: pedido criado via acao (apos L933)
- [ ] **FASE 4e** -- Hook D2: pedido criado via auto-promocao (apos L992)
- [ ] **FASE 4f** -- Hook E: forward recursivo Layer 2 (L966-972)
- [ ] **FASE 5a** -- Extrair `chatwoot_contact_id` no webhook (apos L295)
- [ ] **FASE 5b** -- Passar IDs para `processar_turno` (L364-370)
- [ ] **VERIFICACAO** -- Testar que nada quebrou (testes + manual)

---

## Contexto

O agente 2W Pneus roda um fluxo de vendas de 6 etapas via WhatsApp. Hoje o orquestrador (`_nucleo.py`) e 100% desacoplado do Chatwoot -- nao sabe que a conversa veio de la. O objetivo e criar uma camada de sincronizacao **fail-safe** que, sem alterar nenhuma logica existente e sem tocar no Supabase, projete dados do agente para dentro do Chatwoot (labels, notas privadas, atributos do contato).

**Principio**: se o Chatwoot cair, der timeout ou nao estiver configurado, o agente continua funcionando normalmente.

---

## Arquivos que serao alterados

| Arquivo | Acao | Linhas estimadas |
|---|---|---|
| `agente_2w/config.py` | Adicionar 4 linhas (vars Chatwoot) | +4 |
| `agente_2w/chatwoot_sync.py` | **Criar** (~130 linhas) | +130 |
| `agente_2w/engine/orquestrador/_nucleo.py` | Import + signature + 6 hooks | +25 |
| `webhook_server.py` | Extrair contact_id + passar IDs | +4 |
| **Total** | | **~163 linhas novas** |

**Nenhum arquivo do Supabase, migrations ou schemas sera alterado.**

---

## FASE 0 -- Setup no Chatwoot (via API, antes do codigo)

Criar labels e custom attributes no Chatwoot test instance (account_id obtido do .env).

**Labels** (POST `/api/v1/accounts/{id}/labels`):
- `identificacao`, `buscando`, `oferta_enviada`, `confirmando_item`, `dados_entrega`, `em_fechamento`, `pedido_criado`

**Custom Attributes do Contato** (POST `/api/v1/accounts/{id}/custom_attribute_definitions`):
- `segmento` -- tipo lista (novo / recorrente / vip)
- `total_pedidos` -- tipo numero
- `valor_total_gasto` -- tipo texto (ex: "R$ 1.200,00")
- `ultima_compra` -- tipo texto (ex: "05/04/2026")

---

## FASE 1 -- `agente_2w/config.py` (4 linhas)

**O que**: Centralizar as vars do Chatwoot com defaults seguros.
**Onde**: Apos a linha 17 (`MAX_TOOL_ROUNDS`).
**Por que `os.getenv` e nao `os.environ`**: CLI e testes nao tem essas vars -- `os.environ` crasharia no import.

```python
# --- Chatwoot (opcional -- agente funciona sem) ---
CHATWOOT_BASE_URL: str = os.getenv("CHATWOOT_BASE_URL", "").rstrip("/")
CHATWOOT_API_TOKEN: str = os.getenv("CHATWOOT_API_TOKEN", "")
CHATWOOT_ACCOUNT_ID: str = os.getenv("CHATWOOT_ACCOUNT_ID", "1")
```

> As vars de `webhook_server.py` (linhas 42-46) continuam la -- o webhook **precisa** do Chatwoot pra funcionar, entao `os.environ` (que crasha quando ausente) e correto para ele.

---

## FASE 2 -- `agente_2w/chatwoot_sync.py` (arquivo novo, ~130 linhas)

Modulo central de sincronizacao. Todas as funcoes seguem o padrao:
1. `if not _habilitado(): return` -- no-op quando Chatwoot nao esta configurado
2. `try: ... except Exception: logger.warning(...)` -- nunca levanta excecao
3. `httpx.Client(timeout=8.0)` -- sync (o `_nucleo.py` e sincrono)

### Estrutura do modulo:

```
Imports: logging, httpx, config vars, TYPE_CHECKING para Cliente

Internos:
  _habilitado() -> bool          # retorna False se BASE_URL ou TOKEN vazio
  _headers() -> dict             # {"api_access_token": TOKEN}
  _base() -> str                 # "{BASE_URL}/api/v1/accounts/{ACCOUNT_ID}"

Funcoes publicas (todas fail-safe):
  atualizar_contato(contact_id, dados)                   # PATCH contato
  adicionar_label(conv_id, label)                        # POST label na conversa
  nota_privada(conv_id, texto)                           # POST msg privada
  resolver_conversa(conv_id)                             # PATCH toggle_status
  sincronizar_etapa(conv_id, etapa)                      # mapeia etapa -> label
  sincronizar_nome_cliente(contact_id, nome)             # atualiza nome do contato
  sincronizar_custom_attributes(contact_id, cliente)     # segmento, pedidos, gasto
  sincronizar_pedido_criado(conv_id, numero_pedido, valor_total)  # label + nota
```

### Mapeamento etapa -> label:

```python
_LABEL_POR_ETAPA = {
    "identificacao": "identificacao",
    "busca": "buscando",
    "oferta": "oferta_enviada",
    "confirmacao_item": "confirmando_item",
    "entrega_pagamento": "dados_entrega",
    "fechamento": "em_fechamento",
}
```

### Custom attributes (de `Cliente` schema):

| Campo Supabase | Atributo Chatwoot | Formato |
|---|---|---|
| `cliente.segmento` | `segmento` | "novo", "recorrente", "vip" |
| `cliente.total_pedidos` | `total_pedidos` | int (ex: 5) |
| `cliente.valor_total_gasto` | `valor_total_gasto` | "R$ 1.200,00" |
| `cliente.ultima_compra_em` | `ultima_compra` | "05/04/2026" |

---

## FASE 3 -- `_nucleo.py`: Signature + Import

### 3a. Import (apos linha 37)

```python
from agente_2w import chatwoot_sync
```

### 3b. Signature de `processar_turno` (linhas 755-761)

Adicionar 2 parametros opcionais:

```python
def processar_turno(
    sessao_id: UUID,
    mensagem_texto: str,
    criado_em: datetime | None = None,
    message_id_externo: str | None = None,
    imagens: list[str] | None = None,
    chatwoot_conv_id: int | None = None,        # NOVO
    chatwoot_contact_id: int | None = None,      # NOVO
) -> RespostaTurno:
```

> Defaults `None` = todos os chamadores existentes (CLI, testes) continuam funcionando.

---

## FASE 4 -- `_nucleo.py`: Hooks de sincronizacao (6 pontos de insercao)

Todos os hooks sao **guards com `if`** -- zero overhead quando `chatwoot_*_id` e `None`.

### Hook A -- Custom attributes do cliente (apos linha 800)

**Quando**: Cliente identificado pela primeira vez na sessao.
**Inserir apos** `logger.info("Cliente resolvido: %s", cliente.id)` (L800):

```python
            if chatwoot_contact_id:
                chatwoot_sync.sincronizar_custom_attributes(chatwoot_contact_id, cliente)
```

### Hook B -- Nome do cliente (apos linha 853)

**Quando**: Nome coletado e atualizado no Supabase.
**Inserir apos** `_atualizar_localidade_cliente(...)` (L853):

```python
        if chatwoot_contact_id:
            _fato_nome = contexto_repo.buscar_fato_ativo(sessao_id, ChaveContexto.NOME_CLIENTE)
            if _fato_nome and _fato_nome.valor_texto:
                chatwoot_sync.sincronizar_nome_cliente(chatwoot_contact_id, _fato_nome.valor_texto)
```

### Hook C -- Etapa (label) (apos linha 975)

**Quando**: Transicao de etapa avaliada.
**Inserir apos** `_avaliar_transicao(...)` (L975):

```python
    if chatwoot_conv_id and envelope.etapa_atual != contexto.sessao.etapa_atual:
        chatwoot_sync.sincronizar_etapa(chatwoot_conv_id, envelope.etapa_atual.value)
```

### Hook D1 -- Pedido criado via acao (apos linha 933)

**Quando**: `_despachar_acoes` cria um pedido.
**Inserir apos** `pedido_criado = _despachar_acoes(...)` (L933):

```python
    if chatwoot_conv_id and pedido_criado:
        chatwoot_sync.sincronizar_pedido_criado(
            chatwoot_conv_id, pedido_criado.numero_pedido, pedido_criado.valor_total,
        )
```

### Hook D2 -- Pedido criado via auto-promocao (apos linha 992)

**Quando**: Auto-promocao em fechamento cria pedido.
**Inserir apos** o log `"Auto-promocao em fechamento..."` (L992):

```python
                if chatwoot_conv_id:
                    chatwoot_sync.sincronizar_pedido_criado(
                        chatwoot_conv_id, pedido_criado.numero_pedido, pedido_criado.valor_total,
                    )
```

### Hook E -- Forward recursivo Layer 2 (linhas 966-972)

**Quando**: Nova sessao criada para compra repetida.
**Alterar** a chamada recursiva para incluir os IDs:

```python
            return processar_turno(
                nova_sessao.id,
                mensagem_texto,
                criado_em=criado_em,
                message_id_externo=message_id_externo,
                imagens=imagens,
                chatwoot_conv_id=chatwoot_conv_id,          # NOVO
                chatwoot_contact_id=chatwoot_contact_id,    # NOVO
            )
```

---

## FASE 5 -- `webhook_server.py` (2 alteracoes)

### 5a. Extrair `chatwoot_contact_id` (apos linha 295)

```python
    chatwoot_contact_id = sender_meta.get("id") or sender.get("id")
```

### 5b. Passar IDs para `processar_turno` (linhas 364-370)

```python
            resposta = await asyncio.to_thread(
                processar_turno,
                sessao.id,
                content,
                message_id_externo=message_id,
                imagens=imagens or None,
                chatwoot_conv_id=conversation_id,           # NOVO
                chatwoot_contact_id=chatwoot_contact_id,    # NOVO
            )
```

---

## Diagrama do fluxo completo

```
WhatsApp -> Chatwoot webhook -> webhook_server.py
  extrai: conversation_id, contact_id, telefone
     |
     v
processar_turno(sessao_id, msg, chatwoot_conv_id, chatwoot_contact_id)
     |
     +-- Step 2: cliente identificado
     |    +-> chatwoot_sync.sincronizar_custom_attributes(contact_id, cliente)
     |
     +-- Step 7b: nome coletado
     |    +-> chatwoot_sync.sincronizar_nome_cliente(contact_id, nome)
     |
     +-- Step 10: pedido criado (via acao)
     |    +-> chatwoot_sync.sincronizar_pedido_criado(conv_id, numero, valor)
     |
     +-- Step 11: transicao de etapa
     |    +-> chatwoot_sync.sincronizar_etapa(conv_id, "oferta_enviada")
     |
     +-- Step 12: pedido criado (auto-promocao)
          +-> chatwoot_sync.sincronizar_pedido_criado(conv_id, numero, valor)
```

---

## Verificacao / Testes

1. **Testes existentes nao quebram**: rodar `python -m pytest tests/` -- os testes nao passam `chatwoot_conv_id`, entao defaults `None` garantem que nada muda.

2. **Teste manual via `testar_chatwoot.bat`**:
   - Criar conversa (opcao 1)
   - Enviar mensagem simulando cliente
   - Verificar no painel Chatwoot: labels, notas privadas, custom attributes do contato

3. **Teste ponta-a-ponta**:
   - Enviar mensagem real pelo WhatsApp/Chatwoot
   - Acompanhar no Chatwoot: label muda conforme etapa avanca
   - Quando pedido e criado: label `pedido_criado` + nota com valor

4. **Teste fail-safe**: remover `CHATWOOT_BASE_URL` do .env, rodar o agente, confirmar que funciona normalmente sem erros.

---

## O que NAO sera feito nesta fase

- Webhook reverso (Chatwoot -> Supabase) -- fase futura
- Auto-resolve de conversa quando sessao expira -- fase futura
- Alerta de estoque baixo no Chatwoot -- fase futura
- Auto-assign para agente humano quando sessao bloqueada -- fase futura
- Campanha de reativacao de clientes dormentes -- fase futura
