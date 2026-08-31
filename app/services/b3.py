"""Regras do mercado brasileiro que nao dependem de banco nem de HTTP."""

from __future__ import annotations

import re

from app.models.asset import AssetType

# ETFs listados na B3 terminados em 11. Nao ha endpoint publico e gratuito que
# distinga ETF de FII de UNIT pelo ticker, entao esta lista curada e o unico
# jeito honesto -- e por isso ela fica visivel aqui, e nao escondida num `if`.
ETFS_CONHECIDOS = frozenset(
    {
        "BOVA11",
        "SMAL11",
        "IVVB11",
        "BOVV11",
        "PIBB11",
        "DIVO11",
        "XFIX11",
        "HASH11",
        "GOLD11",
        "IMAB11",
        "B5P211",
        "IRFM11",
        "SPXI11",
        "NASD11",
        "EURP11",
        "ACWI11",
        "FIND11",
        "MATB11",
    }
)

# O prefixo NAO e so de letras: B3SA3 (B3) e M1TA34 (BDR da Meta) sao tickers
# reais da B3. A primeira posicao e sempre letra; as outras tres podem ter
# digito. O sufixo de tipo sao os 1-2 digitos finais.
_TICKER_VALIDO = re.compile(r"^[A-Z][A-Z0-9]{3}\d{1,2}$")
_SUFIXO_TIPO = re.compile(r"\d{1,2}$")


def normalizar_ticker(bruto: str) -> str:
    """ "petr4.sa" -> "PETR4".

    Tira o sufixo de bolsa do Yahoo Finance. Esse sufixo identifica o
    *fornecedor de dados*, nao o ativo -- ele nao pertence ao modelo de dominio.
    """
    return bruto.strip().upper().removesuffix(".SA")


def ticker_valido(ticker: str) -> bool:
    """Formato da B3: quatro letras e um ou dois digitos (PETR4, BOVA11)."""
    return bool(_TICKER_VALIDO.match(ticker))


def classificar(ticker: str, setor: str | None = None) -> AssetType:
    """Deduz o tipo do papel pelo sufixo numerico.

    A B3 codifica o tipo no numero final:
      3     ordinaria (ON)          4,5,6,7,8  preferencial (PN, PNA, PNB...)
      11    unit, FII ou ETF        31..39     BDR

    O 11 e ambiguo de proposito na propria B3 -- resolvemos com o que temos:
    lista curada de ETFs primeiro, depois o setor vindo do fornecedor de dados
    ("Real Estate" indica FII), e o que sobra e tratado como unit.

    E uma heuristica, e esta escrito que e. O alternativo seria uma tabela
    mantida a mao com 400 linhas, que envelhece silenciosamente a cada IPO.
    """
    # Extrai os digitos FINAIS, nao "tudo que nao e letra no comeco" -- senao
    # B3SA3 viraria "3SA3" e cairia em OUTRO.
    encontrado = _SUFIXO_TIPO.search(ticker)
    if encontrado is None:
        return AssetType.OUTRO
    digitos = encontrado.group()

    if digitos in {"3", "4", "5", "6", "7", "8"}:
        return AssetType.ACAO
    if digitos == "11":
        if ticker in ETFS_CONHECIDOS:
            return AssetType.ETF
        if setor and "real estate" in setor.lower():
            return AssetType.FII
        return AssetType.UNIT
    if len(digitos) == 2 and digitos.startswith("3"):
        return AssetType.BDR
    return AssetType.OUTRO
