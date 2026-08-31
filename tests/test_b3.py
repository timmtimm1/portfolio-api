"""Testes das regras do mercado brasileiro (funcoes puras, sem banco)."""

from __future__ import annotations

import pytest

from app.models.asset import AssetType
from app.services.b3 import classificar, normalizar_ticker, ticker_valido


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("PETR4.SA", "PETR4"),
        ("petr4.sa", "PETR4"),
        ("  vale3.SA  ", "VALE3"),
        ("PETR4", "PETR4"),
    ],
)
def test_normaliza_removendo_o_sufixo_do_yahoo(bruto: str, esperado: str) -> None:
    """O `.SA` identifica a bolsa para o Yahoo Finance. E detalhe de fornecedor,
    nao parte da identidade do ativo -- nao pode chegar ao banco."""
    assert normalizar_ticker(bruto) == esperado


@pytest.mark.parametrize(
    ("ticker", "valido"),
    [
        ("PETR4", True),
        ("BOVA11", True),
        ("B3SA3", True),  # digito no prefixo: regressao de um bug real
        ("M1TA34", True),  # BDR da Meta, dois digitos no prefixo
        ("PETR", False),  # sem sufixo de tipo
        ("PET4", False),  # prefixo curto demais
        ("PETR444", False),
        ("'; DROP TABLE assets--", False),
    ],
)
def test_validacao_de_formato(ticker: str, valido: bool) -> None:
    assert ticker_valido(ticker) is valido


@pytest.mark.parametrize(
    ("ticker", "setor", "esperado"),
    [
        ("PETR4", "Energy", AssetType.ACAO),
        ("VALE3", "Basic Materials", AssetType.ACAO),
        ("B3SA3", "Financial Services", AssetType.ACAO),
        ("BOVA11", None, AssetType.ETF),  # lista curada tem prioridade
        ("HGLG11", "Real Estate", AssetType.FII),  # setor resolve a ambiguidade do 11
        ("TAEE11", "Utilities", AssetType.UNIT),  # 11 que nao e FII nem ETF
        ("AAPL34", "Technology", AssetType.BDR),
        ("XPTO9", None, AssetType.OUTRO),
    ],
)
def test_classificacao_pelo_sufixo(ticker: str, setor: str | None, esperado: AssetType) -> None:
    assert classificar(ticker, setor) is esperado


def test_b3sa3_nao_e_confundido_pelo_digito_do_prefixo() -> None:
    """Regressao explicita.

    A primeira versao extraia o sufixo com `re.sub(r"^[A-Z]+", "", ticker)`, o que
    transformava "B3SA3" em "3SA3" e caia em OUTRO. O bug so apareceu ao importar
    os dados reais -- nenhum ticker de exemplo que eu tinha escolhido a mao tinha
    digito no prefixo.
    """
    assert classificar("B3SA3") is AssetType.ACAO
