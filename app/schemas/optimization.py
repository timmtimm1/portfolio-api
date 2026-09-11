"""Schemas da otimizacao de carteira."""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

# Faixa do limite por ativo. Abaixo de 5% a carteira vira uma pulverizacao
# inexecutavel (dezenas de posicoes minusculas); 100% permite concentrar tudo
# num papel, que o usuario pode querer, mas conscientemente.
PESO_MAXIMO_MINIMO = 0.05
PESO_MAXIMO_MAXIMO = 1.0


# Teto de opinioes por pedido. Cada uma vira uma linha de P; acima de meia
# duzia o usuario nao esta mais opinando, esta reescrevendo o mu inteiro na mao
# -- e nesse caso Black-Litterman nao esta fazendo nada por ele.
MAXIMO_VISOES = 6
MAXIMO_ATIVOS_POR_VISAO = 10

# Faixa do retorno de uma opiniao, ao ano. Fora disso e quase sempre erro de
# unidade -- alguem escreveu 15 quando queria 0,15 -- e um retorno de 1500%
# domina o equilibrio inteiro e produz uma carteira sem sentido.
VISAO_RETORNO_MINIMO = -0.95
VISAO_RETORNO_MAXIMO = 3.0


class Visao(BaseModel):
    """Uma opiniao do investidor sobre retorno futuro.

    `ativos` sao os coeficientes da opiniao -- uma linha da matriz P. Dois
    formatos cobrem tudo que se usa na pratica:

    - **Absoluta**: `{"PETR4": 1}` com `retorno: 0.15` -- "PETR4 rende 15% ao
      ano".
    - **Relativa**: `{"PETR4": 1, "VALE3": -1}` com `retorno: 0.03` -- "PETR4
      supera VALE3 em 3 pontos percentuais".

    O coeficiente e livre (`{"ITUB4": 0.5, "BBDC4": 0.5, "VALE3": -1}` opina
    sobre bancos contra mineracao) porque a matematica aceita, e restringir aos
    dois casos comuns fecharia a porta para a opiniao setorial, que e uma das
    mais uteis.

    `retorno` e SEMPRE retorno total anualizado em fracao (0,15 = 15%), inclusive
    na opiniao relativa, onde ele e a diferenca esperada. A conversao para
    retorno excedente acontece no modelo, nao aqui.
    """

    ativos: Annotated[
        dict[str, float],
        Field(
            min_length=1,
            max_length=MAXIMO_ATIVOS_POR_VISAO,
            description='Coeficientes da opiniao. {"PETR4": 1} = absoluta; '
            '{"PETR4": 1, "VALE3": -1} = relativa.',
        ),
    ]
    retorno: Annotated[
        float,
        Field(
            ge=VISAO_RETORNO_MINIMO,
            le=VISAO_RETORNO_MAXIMO,
            description="Retorno anual esperado, em fracao (0.15 = 15% ao ano)",
        ),
    ]

    @field_validator("ativos")
    @classmethod
    def _normaliza(cls, v: dict[str, float]) -> dict[str, float]:
        limpo = {t.strip().upper(): c for t, c in v.items() if t.strip()}
        if not limpo:
            raise ValueError("a opiniao precisa citar pelo menos um ativo")
        # Uma linha toda zerada nao e opiniao nenhuma: ela produziria uma linha
        # nula em P, e Omega = diag(P tau Sigma P') sairia zero naquela posicao
        # -- divisao por zero disfarcada de "confianca infinita".
        if all(c == 0 for c in limpo.values()):
            raise ValueError("os coeficientes da opiniao nao podem ser todos zero")
        return limpo


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
    # Fora de `Annotated` de proposito: com o Field dentro dele, o mypy nao ve
    # default nenhum e passa a exigir `visoes` em toda construcao do schema --
    # `app/routers/metrics.py` quebrou exatamente assim.
    visoes: list[Visao] = Field(
        default_factory=list,
        max_length=MAXIMO_VISOES,
        description="Opinioes sobre retorno futuro. Vazio = so o equilibrio de mercado.",
    )


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


class EquilibrioResumo(BaseModel):
    """De onde saiu o retorno esperado, ANTES das opinioes.

    Existe porque Black-Litterman e uma caixa-preta se a interface so mostrar a
    curva. O usuario precisa poder ver que a carteira sugerida parte dos pesos
    de mercado, e precisa saber quando ela NAO parte disso -- que e o caso
    quando faltou valor de mercado e o modelo caiu para peso igual.
    """

    pesos_mercado: dict[str, float]
    aversao_ao_risco: float
    """delta da otimizacao reversa. Referencia da literatura: ~2,5."""

    usou_peso_igual: bool
    """True quando faltou valor de mercado e o prior deixou de ser o mercado."""

    usou_delta_padrao: bool
    """True quando o delta amostral saiu fora da faixa e caiu no 2,5."""

    sem_valor_de_mercado: list[str]


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

    # O prior de Black-Litterman. Nulo so quando nao houve calculo nenhum.
    equilibrio: EquilibrioResumo | None = None

    visoes_aplicadas: int = 0
    visoes_ignoradas: list[str] = Field(
        default_factory=list,
        description=(
            "Opinioes descartadas por citarem ativo fora do calculo, e o motivo. "
            "Descartar em silencio faria o usuario achar que a opiniao dele pesou."
        ),
    )

    # Por que a fronteira veio vazia. Nulo quando ha resultado.
    #
    # Uma resposta 200 com listas vazias e ambigua: o cliente nao sabe se o
    # calculo nao encontrou nada, se faltou dado ou se a restricao era
    # impossivel -- e acaba mostrando um grafico em branco sem explicacao, que
    # e o pior tipo de erro porque parece defeito do sistema. Dizer o motivo
    # custa um campo e transforma "quebrou" em "faca isto".
    motivo: str | None = None

    aviso: str = Field(
        default=(
            "Retorno esperado por Black-Litterman: parte do equilibrio implicito nos "
            "pesos de mercado e incorpora as opinioes informadas. A covariancia vem do "
            "historico observado, e o premio de risco do mercado tambem -- desempenho "
            "passado nao garante desempenho futuro. As opinioes sao suas: o modelo "
            "propaga o que voce afirmou, nao verifica. Isto nao e recomendacao de "
            "investimento."
        ),
        description="Limitacao do modelo, devolvida sempre",
    )
