"""Cliente do Yahoo Finance -- fornecedor de reserva."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import httpx

from app.clients.base import Cotacao

logger = logging.getLogger(__name__)

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"


class YahooClient:
    """Reserva: sem token e sem cota declarada, mas sem contrato de servico.

    Nao e a fonte primaria justamente por isso -- e um endpoint nao documentado
    que ja mudou de formato mais de uma vez. Serve para o caso em que a brapi
    esta fora ou a cota do mes acabou.

    Aqui as consultas sao uma por ticker (o endpoint nao aceita lote), e por isso
    ele e o segundo da fila: so recebe o que o primario nao conseguiu.
    """

    nome = "yahoo"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def cotacoes(self, tickers: Sequence[str]) -> dict[str, Cotacao]:
        cotacoes: dict[str, Cotacao] = {}
        for ticker in tickers:
            cotacao = await self._buscar(ticker)
            if cotacao is not None:
                cotacoes[ticker] = cotacao
        return cotacoes

    async def _buscar(self, ticker: str) -> Cotacao | None:
        # O sufixo ".SA" e adicionado AQUI, no adaptador -- nunca guardado no
        # banco. E a convencao deste fornecedor para a B3, nao parte do ativo.
        try:
            resposta = await self._client.get(
                f"{BASE_URL}/{ticker}.SA", params={"range": "1d", "interval": "1d"}
            )
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[yahoo] falha ao buscar %s: %s", ticker, type(exc).__name__)
            return None

        try:
            meta = dados["chart"]["result"][0]["meta"]
            bruto = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
            valor = Decimal(str(bruto))
        except (KeyError, IndexError, TypeError, InvalidOperation):
            return None

        return Cotacao(ticker, valor, "yahoo") if valor > 0 else None
