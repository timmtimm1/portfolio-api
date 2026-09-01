"""Cliente do SGS -- Sistema Gerenciador de Series Temporais do Banco Central.

Fonte oficial, publica, sem token e sem cota declarada. E a referencia correta
para CDI, Selic e IPCA: usar um numero de terceiro para comparar rentabilidade
seria introduzir uma divergencia que ninguem consegue explicar depois.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.models.benchmark import Indexador

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bcb.gov.br/dados/serie"

# Codigos das series no SGS.
SERIES = {Indexador.CDI: 12, Indexador.SELIC: 11, Indexador.IPCA: 433}

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
        serie fora do ar. Comparar com um indexador e um extra: a carteira do
        usuario continua sendo exibida sem ele.
        """
        if (ate - desde).days > JANELA_MAXIMA_DIAS:
            desde = date_type.fromordinal(ate.toordinal() - JANELA_MAXIMA_DIAS)

        busca_desde = desde
        if indexador is Indexador.IPCA:
            # O IPCA data cada valor no dia 1 do mes de referencia. Se `desde`
            # cai no meio do mes (dia 20, por exemplo), filtrar a partir dele
            # excluiria o proprio mes corrente -- o valor de agosto, datado
            # "01/08", ficaria fora de uma janela que comeca em "20/08".
            busca_desde = desde.replace(day=1)

        url = f"{BASE_URL}/bcdata.sgs.{SERIES[indexador]}/dados"
        params = {
            "formato": "json",
            # O SGS so aceita dd/MM/yyyy. Mandar ISO devolve a serie inteira
            # desde 1986 em silencio -- alguns megabytes por um formato errado.
            "dataInicial": busca_desde.strftime("%d/%m/%Y"),
            "dataFinal": ate.strftime("%d/%m/%Y"),
        }

        try:
            resposta = await self._client.get(url, params=params)
            resposta.raise_for_status()
            dados = resposta.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[bcb] falha ao buscar %s: %s", indexador, type(exc).__name__)
            return {}

        mensal_ou_diario = self._extrair(dados)
        if indexador is Indexador.IPCA:
            return self._espalhar_mensal_em_diario(mensal_ou_diario)
        return mensal_ou_diario

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

    @staticmethod
    def _espalhar_mensal_em_diario(mensal: dict[date_type, Decimal]) -> dict[date_type, Decimal]:
        """Converte a variacao MENSAL do IPCA em uma taxa diaria equivalente.

        `_extrair()` ja devolveu `{1o_dia_do_mes: fracao_do_mes}` -- o SGS
        publica um unico valor por mes, ao contrario de CDI/Selic, que sao um
        por dia util. Em vez de ensinar `curva_equivalente()` e
        `taxas_do_periodo()` a reconhecer um segundo formato, resolvemos aqui,
        na borda: achamos a raiz n-esima do fator mensal (n = dias corridos do
        mes) e repetimos esse mesmo valor em todo dia do mes. Composto ao longo
        do mes inteiro, reproduz exatamente a variacao publicada -- e o resto
        do sistema recebe uma taxa diaria comum, sem saber que ela veio de um
        numero so.

        Uso `Decimal.ln()`/`.exp()` para a raiz fracionaria em vez de passar por
        `float`: `BenchmarkRate.rate` e dinheiro-adjacente (taxa que compoe
        sobre uma carteira em R$), entao segue a mesma convencao de manter
        Decimal ate a fronteira com estatistica (metrics.py), que esta nao e.
        """
        diario: dict[date_type, Decimal] = {}
        for referencia, taxa_mensal in mensal.items():
            fator_mensal = 1 + taxa_mensal
            if fator_mensal <= 0:
                # Deflacao mensal >= 100% e impossivel na pratica; um valor
                # corrompido do fornecedor nao pode virar ln() de nao-positivo.
                continue
            dias_no_mes = calendar.monthrange(referencia.year, referencia.month)[1]
            try:
                taxa_diaria = (fator_mensal.ln() / dias_no_mes).exp() - 1
            except InvalidOperation:
                continue
            for dia in range(1, dias_no_mes + 1):
                diario[referencia.replace(day=dia)] = taxa_diaria
        return diario
