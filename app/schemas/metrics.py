"""Schemas das metricas de risco."""

from __future__ import annotations

from datetime import date as date_type

from pydantic import BaseModel, Field


class AssetMetrics(BaseModel):
    """Metricas de um ativo.

    Todos os valores sao `float` e estao rotulados como estatistica, nao como
    dinheiro. Retornos e volatilidade vem em fracao decimal (0.2341 = 23,41%),
    nao em percentual: converter para percentual e decisao de apresentacao, e
    fazer isso na API obrigaria todo cliente a saber qual convencao foi usada.
    """

    ticker: str
    observacoes: int = Field(description="Pregoes usados no calculo")
    retorno_periodo: float
    retorno_anualizado: float
    volatilidade_anualizada: float
    indice_sharpe: float | None
    maior_queda: float = Field(description="Maximum drawdown, negativo")


class CorrelationMatrix(BaseModel):
    tickers: list[str]
    # Matriz quadrada na ordem de `tickers`. Lista de listas, nao dicionario
    # aninhado: preserva a ordem e e o formato que qualquer biblioteca de
    # grafico consome direto.
    matriz: list[list[float]]


class PortfolioMetrics(BaseModel):
    inicio: date_type | None
    fim: date_type | None
    pregoes: int
    taxa_livre_risco: float
    ativos: list[AssetMetrics]
    correlacao: CorrelationMatrix | None
    # Tickers pedidos que ficaram de fora, e por que. Silenciar isso faria o
    # usuario acreditar que a analise cobriu a carteira inteira.
    sem_historico_suficiente: list[str]
