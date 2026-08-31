"""Fabricas dos clientes externos."""

from __future__ import annotations

from functools import lru_cache

import httpx

from app.clients.base import ProvedorDeCotacoes
from app.clients.bcb import BcbClient
from app.clients.brapi import BrapiClient
from app.clients.composto import ProvedorEncadeado
from app.clients.yahoo import YahooClient
from app.core.config import get_settings

__all__ = [
    "fechar_http_client",
    "get_bcb_client",
    "get_http_client",
    "get_provedor_de_cotacoes",
]


@lru_cache
def get_http_client() -> httpx.AsyncClient:
    """Um unico cliente HTTP por processo, com pool de conexoes reaproveitado.

    Criar um `AsyncClient` por chamada e um erro caro: cada um abre conexoes
    novas, refaz o handshake TLS e nunca as fecha -- vazamento de descritores de
    arquivo ate o processo morrer.
    """
    settings = get_settings()
    return httpx.AsyncClient(
        # Timeouts separados por fase. Um `timeout` unico e grosseiro: conectar
        # deve ser rapido (se nao conectou em 3s, nao vai conectar), mas ler pode
        # ser mais lento. Sem timeout NENHUM -- que e o padrao de varias
        # bibliotecas -- um fornecedor que aceita a conexao e nunca responde
        # prende o worker para sempre.
        timeout=httpx.Timeout(
            connect=3.0,
            read=settings.HTTP_TIMEOUT_SECONDS,
            write=3.0,
            pool=3.0,
        ),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        # Nao seguimos redirecionamento: um 3xx inesperado de uma API de dados e
        # sinal de problema (portal cativo, bloqueio, dominio sequestrado), nao
        # algo a obedecer cegamente.
        follow_redirects=False,
        headers={"User-Agent": "portfolio-api/0.1 (+https://github.com/timmtimm1/portfolio-api)"},
    )


@lru_cache
def get_provedor_de_cotacoes() -> ProvedorDeCotacoes:
    """brapi como primario, Yahoo completando as lacunas."""
    settings = get_settings()
    client = get_http_client()
    token = settings.BRAPI_TOKEN.get_secret_value() if settings.BRAPI_TOKEN else None
    return ProvedorEncadeado(BrapiClient(client, token), YahooClient(client))


@lru_cache
def get_bcb_client() -> BcbClient:
    """Cliente do Banco Central, compartilhando o mesmo pool HTTP."""
    return BcbClient(get_http_client())


async def fechar_http_client() -> None:
    """Fecha o pool no shutdown."""
    if get_http_client.cache_info().currsize:
        await get_http_client().aclose()
