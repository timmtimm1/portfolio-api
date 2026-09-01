"""Cliente do Yahoo Finance -- fornecedor de reserva."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal, InvalidOperation

import httpx

from app.clients.base import Cotacao, DesdobramentoBruto, ProventoBruto

logger = logging.getLogger(__name__)

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

# O endpoint so aceita janela relativa, nao data inicial e final. Pedimos uma
# janela larga e filtramos em memoria -- 10 anos cobre qualquer carteira de
# pessoa fisica, e o resultado e cacheado no banco, entao a chamada e rara.
JANELA_PROVENTOS = "10y"


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

    async def proventos(self, ticker: str, desde: date_type, ate: date_type) -> list[ProventoBruto]:
        """Dividendos e JCP anunciados no periodo, por cota.

        Mesmo endpoint da cotacao, com `events=div`: o Yahoo devolve os eventos
        de provento junto com a serie. Cada evento traz a DATA-COM (nao a de
        pagamento) e o valor bruto por cota -- e nao diz se foi dividendo ou
        JCP, o que fica a cargo de quem grava.

        Devolve lista vazia em qualquer falha, mesma regra do resto do modulo:
        um ativo sem provento e um fornecedor fora do ar produzem o mesmo
        resultado visivel, e nenhum dos dois pode derrubar a carteira.
        """
        try:
            resposta = await self._client.get(
                f"{BASE_URL}/{ticker}.SA",
                params={"range": JANELA_PROVENTOS, "interval": "1d", "events": "div"},
            )
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "[yahoo] falha ao buscar proventos de %s: %s", ticker, type(exc).__name__
            )
            return []

        return self._extrair_proventos(dados, desde, ate)

    @staticmethod
    def _extrair_proventos(dados: object, desde: date_type, ate: date_type) -> list[ProventoBruto]:
        """Le `chart.result[0].events.dividends`, que e um dicionario com o
        timestamp como CHAVE e `{amount, date}` como valor."""
        try:
            eventos = dados["chart"]["result"][0]["events"]["dividends"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return []
        if not isinstance(eventos, dict):
            return []

        proventos: list[ProventoBruto] = []
        for evento in eventos.values():
            if not isinstance(evento, dict):
                continue
            try:
                # O timestamp cai durante o pregao (13h-21h UTC, horario da B3
                # em UTC-3) e nunca cruza a meia-noite UTC, entao a data em UTC
                # e a data-com correta sem converter fuso.
                dia = datetime.fromtimestamp(evento["date"], tz=UTC).date()
                valor = Decimal(str(evento["amount"]))
            except (KeyError, TypeError, ValueError, InvalidOperation, OSError):
                continue
            if valor > 0 and desde <= dia <= ate:
                proventos.append(ProventoBruto(dia, valor))

        proventos.sort(key=lambda p: p.data_com)
        return proventos

    async def desdobramentos(
        self, ticker: str, desde: date_type, ate: date_type
    ) -> list[DesdobramentoBruto]:
        """Desdobramentos, grupamentos e bonificacoes anunciados no periodo.

        Mesmo endpoint dos proventos, com `events=split`. O Yahoo devolve os
        tres como "split", com numerador e denominador: 2:1 e desdobramento,
        1:10 e grupamento, 103:100 e bonificacao de 3%. A distincao e de nome,
        nao de matematica -- todos multiplicam a quantidade por num/den.

        Uma requisicao separada da de proventos, e nao `events=div,split` numa
        so: os dois metodos tem chamadores diferentes e cada um paga so pelo
        que usa. A sincronizacao, que precisa dos dois, e rara e limitada.
        """
        try:
            resposta = await self._client.get(
                f"{BASE_URL}/{ticker}.SA",
                params={"range": JANELA_PROVENTOS, "interval": "1d", "events": "split"},
            )
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "[yahoo] falha ao buscar desdobramentos de %s: %s", ticker, type(exc).__name__
            )
            return []

        return self._extrair_desdobramentos(dados, desde, ate)

    @staticmethod
    def _extrair_desdobramentos(
        dados: object, desde: date_type, ate: date_type
    ) -> list[DesdobramentoBruto]:
        """Le `chart.result[0].events.splits`, no mesmo formato de dicionario
        indexado por timestamp que os proventos usam."""
        try:
            eventos = dados["chart"]["result"][0]["events"]["splits"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return []
        if not isinstance(eventos, dict):
            return []

        desdobramentos: list[DesdobramentoBruto] = []
        for evento in eventos.values():
            if not isinstance(evento, dict):
                continue
            try:
                dia = datetime.fromtimestamp(evento["date"], tz=UTC).date()
                numerador = Decimal(str(evento["numerator"]))
                denominador = Decimal(str(evento["denominator"]))
            except (KeyError, TypeError, ValueError, InvalidOperation, OSError):
                continue
            # Denominador zero tornaria o fator infinito e corromperia toda a
            # posicao do ativo. Numerador zero zeraria a carteira. Nenhum dos
            # dois existe na realidade -- se vier, o dado esta corrompido.
            if numerador > 0 and denominador > 0 and desde <= dia <= ate:
                desdobramentos.append(DesdobramentoBruto(dia, numerador, denominador))

        desdobramentos.sort(key=lambda d: d.data_ex)
        return desdobramentos
