"""Cliente do indice Ibovespa -- via Yahoo Finance, nao via BCB.

O SGS do Banco Central chegou a publicar o Ibovespa (serie 7), mas a
descontinuou em 2019 -- pedir um periodo de hoje devolve "Value(s) not found".
O Yahoo Finance e o mesmo fornecedor de reserva que `YahooClient` ja usa para
cotacao de acoes, e o unico dos dois testado que ainda devolve o indice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
TICKER = "%5EBVSP"  # "^BVSP" com o "^" percent-encoded.

# O Yahoo nao aceita `dataInicial`/`dataFinal` como o SGS -- so uma janela
# relativa ("range"). Pedimos sempre a mesma janela generosa e filtramos em
# memoria; o cache em `benchmark_rates` (ver `taxas_do_periodo`) evita repetir
# isso a cada request, entao o custo extra so aparece na primeira vez que um
# dia novo e pedido.
RANGE = "5y"


class IbovClient:
    """Ibovespa como uma serie de taxas DIARIAS, no mesmo formato de `BcbClient`.

    Nao guarda o nivel do indice (178.000 pontos nao significa nada sozinho) --
    guarda a variacao percentual de um fechamento para o outro, que e o que
    `curva_equivalente()` espera: uma taxa que compoe dia a dia.
    """

    nome = "yahoo"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def variacoes_diarias(self, desde: date_type, ate: date_type) -> dict[date_type, Decimal]:
        """Variacao percentual diaria do Ibovespa, em FRACAO decimal.

        Devolve dicionario vazio em qualquer falha -- mesma regra do `BcbClient`:
        comparar com um indice e um extra, a carteira continua sendo exibida
        sem ele. `desde`/`ate` filtram o resultado, mas nao mudam o que e
        pedido ao Yahoo (ver `RANGE`).
        """
        try:
            resposta = await self._client.get(
                f"{BASE_URL}/{TICKER}", params={"range": RANGE, "interval": "1d"}
            )
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[ibov] falha ao buscar: %s", type(exc).__name__)
            return {}

        fechamentos = self._extrair_fechamentos(dados)
        return self._variacoes(fechamentos, desde, ate)

    @staticmethod
    def _extrair_fechamentos(dados: object) -> list[tuple[date_type, Decimal]]:
        """Le a resposta defensivamente: `timestamp[]` e `close[]` andam em
        paralelo, e o Yahoo pode intercalar `None` num pregao sem fechamento
        registrado (feriado que o calendario da bolsa nao previa, falha do
        proprio fornecedor)."""
        try:
            resultado = dados["chart"]["result"][0]  # type: ignore[index]
            timestamps = resultado["timestamp"]
            fechamentos_brutos = resultado["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError):
            return []
        if not isinstance(timestamps, list) or not isinstance(fechamentos_brutos, list):
            return []

        pontos: list[tuple[date_type, Decimal]] = []
        for ts, bruto in zip(timestamps, fechamentos_brutos, strict=False):
            if bruto is None:
                continue
            try:
                # O timestamp e um instante durante o pregao (13h-21h UTC, que e
                # o horario da B3 em UTC-3) -- nunca cruza a meia-noite UTC, entao
                # tomar a data em UTC direto e seguro, sem converter fuso.
                dia = datetime.fromtimestamp(ts, tz=UTC).date()
                valor = Decimal(str(bruto))
            except (TypeError, ValueError, InvalidOperation, OSError):
                continue
            if valor > 0:
                pontos.append((dia, valor))
        return pontos

    @staticmethod
    def _variacoes(
        fechamentos: list[tuple[date_type, Decimal]], desde: date_type, ate: date_type
    ) -> dict[date_type, Decimal]:
        """Fechamento de hoje sobre o de ontem, menos 1 -- a mesma conta de
        `retorno = valor[t]/valor[t-1] - 1` que o resto do projeto usa.

        Devolve so as datas dentro de [`desde`, `ate`], mas CALCULA usando o
        fechamento anterior mesmo que ele caia fora da janela -- e por isso
        `RANGE` busca mais do que o pedido: sem um ponto anterior, o primeiro
        dia da janela pedida ficaria sem taxa nenhuma.
        """
        fechamentos.sort(key=lambda par: par[0])
        variacoes: dict[date_type, Decimal] = {}
        for (_, anterior), (dia, atual) in zip(fechamentos, fechamentos[1:], strict=False):
            if desde <= dia <= ate:
                variacoes[dia] = atual / anterior - 1
        return variacoes
