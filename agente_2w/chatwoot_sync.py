"""Sincronizacao com Chatwoot — atualiza contato, labels e notas em tempo real.

Todas as funcoes sao fail-safe: se o Chatwoot estiver fora do ar, com token
invalido ou nao configurado, retornam silenciosamente sem lancar excecao.
O agente nunca quebra por causa do Chatwoot.

Uso:
    from agente_2w import chatwoot_sync

    chatwoot_sync.sincronizar_etapa(conv_id, "oferta")
    chatwoot_sync.sincronizar_custom_attributes(contact_id, cliente)
    chatwoot_sync.sincronizar_pedido_criado(conv_id, 42, Decimal("950.00"))
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

from agente_2w.config import CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_BASE_URL

if TYPE_CHECKING:
    from agente_2w.schemas.cliente import Cliente

logger = logging.getLogger(__name__)

# Mapeamento etapa do agente -> label no Chatwoot
_LABEL_POR_ETAPA: dict[str, str] = {
    "identificacao": "identificacao",
    "busca": "buscando",
    "oferta": "oferta_enviada",
    "confirmacao_item": "confirmando_item",
    "entrega_pagamento": "dados_entrega",
    "fechamento": "em_fechamento",
}

# Cliente HTTP reutilizavel (singleton lazy)
_http: httpx.Client | None = None


def _habilitado() -> bool:
    """Retorna True apenas se BASE_URL e TOKEN estao configurados."""
    return bool(CHATWOOT_BASE_URL and CHATWOOT_API_TOKEN)


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=8.0)
    return _http


def _headers() -> dict[str, str]:
    return {"api_access_token": CHATWOOT_API_TOKEN, "Content-Type": "application/json"}


def _base() -> str:
    return f"{CHATWOOT_BASE_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}"


# ---------------------------------------------------------------------------
# Funcoes publicas
# ---------------------------------------------------------------------------

def atualizar_contato(contact_id: int, dados: dict) -> None:
    """Atualiza campos do contato no Chatwoot (nome, custom_attributes, etc.)."""
    if not _habilitado() or not contact_id:
        return
    try:
        url = f"{_base()}/contacts/{contact_id}"
        resp = _client().patch(url, json=dados, headers=_headers())
        resp.raise_for_status()
        logger.debug("Contato %s atualizado no Chatwoot", contact_id)
    except Exception:
        logger.warning("Falha ao atualizar contato %s no Chatwoot", contact_id, exc_info=True)


def adicionar_label(conv_id: int, label: str) -> None:
    """Adiciona uma label a conversa sem remover as existentes."""
    if not _habilitado() or not conv_id:
        return
    try:
        url = f"{_base()}/conversations/{conv_id}/labels"
        # GET labels atuais para nao sobrescrever
        resp = _client().get(url, headers=_headers())
        resp.raise_for_status()
        labels_atuais: list[str] = resp.json().get("payload", [])
        if label in labels_atuais:
            return  # ja existe, nada a fazer
        labels_atuais.append(label)
        _client().post(url, json={"labels": labels_atuais}, headers=_headers()).raise_for_status()
        logger.info("Label '%s' adicionada na conv %d", label, conv_id)
    except Exception:
        logger.warning("Falha ao adicionar label '%s' na conv %d", label, conv_id, exc_info=True)


def nota_privada(conv_id: int, texto: str) -> None:
    """Cria uma nota privada (nao visivel ao cliente) na conversa."""
    if not _habilitado() or not conv_id:
        return
    try:
        url = f"{_base()}/conversations/{conv_id}/messages"
        payload = {"content": texto, "message_type": "outgoing", "private": True}
        resp = _client().post(url, json=payload, headers=_headers())
        resp.raise_for_status()
        logger.debug("Nota privada criada na conv %d", conv_id)
    except Exception:
        logger.warning("Falha ao criar nota privada na conv %d", conv_id, exc_info=True)


def resolver_conversa(conv_id: int) -> None:
    """Marca a conversa como resolvida no Chatwoot."""
    if not _habilitado() or not conv_id:
        return
    try:
        url = f"{_base()}/conversations/{conv_id}/toggle_status"
        resp = _client().patch(url, json={"status": "resolved"}, headers=_headers())
        resp.raise_for_status()
        logger.info("Conversa %d marcada como resolvida", conv_id)
    except Exception:
        logger.warning("Falha ao resolver conversa %d", conv_id, exc_info=True)


def sincronizar_etapa(conv_id: int, etapa: str) -> None:
    """Adiciona a label correspondente a etapa atual do fluxo de vendas."""
    label = _LABEL_POR_ETAPA.get(etapa)
    if not label:
        logger.debug("Etapa '%s' sem label mapeada — ignorando sync", etapa)
        return
    adicionar_label(conv_id, label)


def sincronizar_nome_cliente(contact_id: int, nome: str) -> None:
    """Atualiza o nome do contato no Chatwoot."""
    if not nome:
        return
    atualizar_contato(contact_id, {"name": nome})


def sincronizar_custom_attributes(contact_id: int, cliente: Cliente) -> None:
    """Envia segmento, historico de compras e ultima compra pro perfil do contato."""
    if not _habilitado() or not contact_id:
        return
    try:
        attrs: dict = {
            "segmento": cliente.segmento,
            "total_pedidos": cliente.total_pedidos,
            "valor_total_gasto": f"R$ {cliente.valor_total_gasto:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
        }
        if cliente.ultima_compra_em:
            attrs["ultima_compra"] = cliente.ultima_compra_em.strftime("%d/%m/%Y")
        atualizar_contato(contact_id, {"custom_attributes": attrs})
        logger.debug("Custom attributes sincronizados pro contato %s", contact_id)
    except Exception:
        logger.warning("Falha ao sincronizar custom attributes do contato %s", contact_id, exc_info=True)


def sincronizar_pedido_criado(conv_id: int, numero_pedido: int | str, valor_total: Decimal | float) -> None:
    """Adiciona label pedido_criado e nota privada com resumo do pedido."""
    adicionar_label(conv_id, "pedido_criado")
    valor_fmt = f"R$ {float(valor_total):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    nota_privada(conv_id, f"Pedido #{numero_pedido} criado — {valor_fmt}")
