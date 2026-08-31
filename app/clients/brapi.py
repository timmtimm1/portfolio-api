"""Cliente da brapi.dev -- fornecedor primario de cotacoes da B3."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.clients.base import Cotacao

logger = logging.getLogger(__name__)

BASE_URL = "https://brapi.dev/api/quote"

# Teto de tickers por requisicao. O plano gratuito aceita poucos por chamada;
# lotes maiores sao recusados inteiros -- perder a resposta toda por excesso e
# pior do que fazer duas chamadas.
LOTE_MAXIMO = 10


class BrapiClient:
    """Fonte primaria. Plano gratuito: 15 mil requisicoes/mes, com atraso.

    O atraso nao e problema para esta aplicacao: acompanhamento de carteira nao
    e day trade. E o motivo de o cache poder ter TTL generoso.
    """

    nome = "brapi"

    def __init__(self, client: httpx.AsyncClient, token: str | None = None) -> None:
        self._client = client
        self._token = token

    async def cotacoes(self, tickers: Sequence[str]) -> dict[str, Cotacao]:
        resultado: dict[str, Cotacao] = {}
        for inicio in range(0, len(tickers), LOTE_MAXIMO):
            lote = tickers[inicio : inicio + LOTE_MAXIMO]
            resultado.update(await self._buscar_lote(lote))
        return resultado

    async def _buscar_lote(self, tickers: Sequence[str]) -> dict[str, Cotacao]:
        # Os tickers chegam aqui ja validados contra o formato da B3 (a coluna do
        # banco so contem tickers que passaram por `ticker_valido`). Ainda assim
        # eles vao no PATH da URL, entao a validacao a montante e o que impede
        # que um valor inesperado altere a rota chamada -- SSRF por interpolacao
        # de caminho e uma falha real, nao teorica.
        url = f"{BASE_URL}/{','.join(tickers)}"
        params: dict[str, Any] = {"token": self._token} if self._token else {}

        try:
            resposta = await self._client.get(url, params=params)
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Log sem os parametros: o token vai na query string, e um log de erro
            # com a URL completa vazaria a credencial.
            logger.warning("[brapi] falha ao buscar %s: %s", tickers, type(exc).__name__)
            return {}

        return self._extrair(dados)

    @staticmethod
    def _extrair(dados: object) -> dict[str, Cotacao]:
        """Le a resposta defensivamente.

        Nunca confie no formato de uma API externa: um campo que some numa
        atualizacao do fornecedor viraria KeyError e um 500 para o usuario. Aqui
        o que nao vier no formato esperado e simplesmente ignorado.
        """
        if not isinstance(dados, dict):
            return {}
        resultados = dados.get("results")
        if not isinstance(resultados, list):
            return {}

        cotacoes: dict[str, Cotacao] = {}
        for item in resultados:
            if not isinstance(item, dict):
                continue
            simbolo = item.get("symbol")
            preco = item.get("regularMarketPrice")
            if not isinstance(simbolo, str) or preco is None:
                continue
            try:
                valor = Decimal(str(preco))
            except (InvalidOperation, TypeError):
                continue
            if valor <= 0:
                continue  # preco zero ou negativo e dado corrompido, nao cotacao
            cotacoes[simbolo.upper()] = Cotacao(simbolo.upper(), valor, "brapi")
        return cotacoes
