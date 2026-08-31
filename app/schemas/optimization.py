"""Schemas da otimizacao de carteira."""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from pydantic import BaseModel, Field

# Faixa do limite por ativo. Abaixo de 5% a carteira vira uma pulverizacao
# inexecutavel (dezenas de posicoes minusculas); 100% permite concentrar tudo
# num papel, que o usuario pode querer, mas conscientemente.
PESO_MAXIMO_MINIMO = 0.05
PESO_MAXIMO_MAXIMO = 1.0


class OptimizationRequest(BaseModel):
    tickers: Annotated[
        list[str] | None,
        Field(
            default=None,
            max_length=30,
            description="Ativos a considerar. Vazio = os que voce tem em carteira.",
        ),
    ] = None
    peso_maximo: Annotated[float, Field(ge=PESO_MAXIMO_MINIMO, le=PESO_MAXIMO_MAXIMO)] = 0.40
    pontos: Annotated[int, Field(ge=5, le=100, description="Pontos da fronteira")] = 50
    desde: date_type | None = None
    ate: date_type | None = None


class CarteiraSugerida(BaseModel):
    """Uma carteira: pesos e as metricas dela.

    Pesos em fracao (0.25 = 25%), somando 1. Fracao e nao percentual pela mesma
    razao das metricas: converter e decisao de apresentacao, e fazer isso na API
    obrigaria todo cliente a saber qual convencao foi usada.
    """

    pesos: dict[str, float]
    retorno_esperado: float
    volatilidade: float
    indice_sharpe: float | None


class OptimizationResponse(BaseModel):
    inicio: date_type | None
    fim: date_type | None
    pregoes: int
    taxa_livre_risco: float
    peso_maximo: float
    tickers: list[str]

    fronteira: list[CarteiraSugerida]
    minima_variancia: CarteiraSugerida | None
    maximo_sharpe: CarteiraSugerida | None
    # A carteira do usuario avaliada com os MESMOS parametros, para cair no mesmo
    # grafico. E a comparacao que de fato interessa: "a minha esta longe da
    # fronteira?". Nula quando ele pediu ativos que nao possui.
    carteira_atual: CarteiraSugerida | None

    sem_historico_suficiente: list[str]

    aviso: str = Field(
        default=(
            "Resultado baseado em retorno e covariancia estimados sobre o historico "
            "observado. Desempenho passado nao garante desempenho futuro, e a "
            "estimativa de retorno esperado e instavel. Isto nao e recomendacao de "
            "investimento."
        ),
        description="Limitacao do modelo, devolvida sempre",
    )
