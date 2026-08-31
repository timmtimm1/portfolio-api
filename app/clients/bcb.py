"""Cliente do SGS -- Sistema Gerenciador de Series Temporais do Banco Central.

Fonte oficial, publica, sem token e sem cota declarada. E a referencia correta
para CDI e Selic: usar um numero de terceiro para comparar rentabilidade seria
introduzir uma divergencia que ninguem consegue explicar depois.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.models.benchmark import Indexador

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bcb.gov.br/dados/serie"

# Codigos das series no SGS.
SERIES = {Indexador.CDI: 12, Indexador.SELIC: 11}

# O SGS recusa janelas muito longas em algumas series. Dez anos cobre qualquer
# carteira de pessoa fisica e fica bem dentro do limite.
JANELA_MAXIMA_DIAS = 3650


class BcbClient:
    nome = "bcb"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def taxas(
        self, indexador: Indexador, desde: date_type, ate: date_type
    ) -> dict[date_type, Decimal]:
        """Taxas diarias no periodo, em FRACAO decimal.

        Devolve dicionario vazio em qualquer falha -- rede, formato inesperado,
        serie fora do ar. Comparar com o CDI e um extra: a carteira do usuario
        continua sendo exibida sem ele.
        """
        if (ate - desde).days > JANELA_MAXIMA_DIAS:
            desde = date_type.fromordinal(ate.toordinal() - JANELA_MAXIMA_DIAS)

        url = f"{BASE_URL}/bcdata.sgs.{SERIES[indexador]}/dados"
        params = {
            "formato": "json",
            # O SGS so aceita dd/MM/yyyy. Mandar ISO devolve a serie inteira
            # desde 1986 em silencio -- alguns megabytes por um formato errado.
            "dataInicial": desde.strftime("%d/%m/%Y"),
            "dataFinal": ate.strftime("%d/%m/%Y"),
        }

        try:
            resposta = await self._client.get(url, params=params)
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[bcb] falha ao buscar %s: %s", indexador, type(exc).__name__)
            return {}

        return self._extrair(dados)

    @staticmethod
    def _extrair(dados: object) -> dict[date_type, Decimal]:
        """Le a resposta defensivamente e converte percentual em fracao."""
        if not isinstance(dados, list):
            return {}

        taxas: dict[date_type, Decimal] = {}
        for item in dados:
            if not isinstance(item, dict):
                continue
            bruto_data, bruto_valor = item.get("data"), item.get("valor")
            if not isinstance(bruto_data, str) or not isinstance(bruto_valor, str):
                continue
            try:
                # `datetime.strptime`, nao `date.strptime` -- este ultimo nao
                # existe. Um `# type: ignore` aqui teria escondido o AttributeError
                # ate o primeiro uso em producao.
                dia = datetime.strptime(bruto_data, "%d/%m/%Y").date()  # noqa: DTZ007
            except ValueError:
                continue
            try:
                # A conversao percentual -> fracao acontece AQUI, uma unica vez,
                # na fronteira com o fornecedor.
                taxas[dia] = Decimal(bruto_valor) / 100
            except (InvalidOperation, TypeError):
                continue
        return taxas
