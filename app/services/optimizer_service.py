"""Orquestracao da otimizacao: carrega series, monta mu e Sigma, otimiza."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.optimization import CarteiraSugerida, OptimizationRequest, OptimizationResponse
from app.services import series_service, transaction_service
from app.services.metrics import (
    MINIMO_OBSERVACOES,
    SeriesAlinhadas,
    matriz_covariancia,
    retorno_anualizado,
)
from app.services.optimizer import (
    Carteira,
    OtimizacaoInviavelError,
    carteira_para,
    fronteira_eficiente,
    maximo_sharpe,
    minima_variancia,
)
from app.services.position import Posicao

# Minimo de ativos para haver o que otimizar. Com um so, o resultado e "100%
# nele" -- resposta correta e inutil.
MINIMO_ATIVOS = 2


def _sugerida(carteira: Carteira, tickers: list[str]) -> CarteiraSugerida:
    """Converte pesos posicionais em pesos NOMEADOS.

    O otimizador trabalha com indices; a API devolve nomes. Fazer essa traducao
    num unico lugar evita o pior bug possivel aqui: pesos certos atribuidos aos
    ativos errados, que ninguem percebe porque os numeros parecem plausiveis.
    """
    return CarteiraSugerida(
        pesos={t: float(p) for t, p in zip(tickers, carteira.pesos, strict=True)},
        retorno_esperado=carteira.retorno_esperado,
        volatilidade=carteira.volatilidade,
        indice_sharpe=carteira.indice_sharpe,
    )


def _retornos_esperados(series: SeriesAlinhadas, tickers: list[str]) -> np.ndarray:
    """Retorno anualizado historico como estimativa do esperado.

    E a estimativa mais ingenua que existe, e esta assim de proposito: e o
    Markowitz classico. Vale saber que ela e a parte fraca do modelo -- pequenas
    mudancas na janela produzem carteiras bem diferentes. Alternativas (Black-
    Litterman, shrinkage de James-Stein) atacam exatamente esse ponto.
    """
    return np.array([retorno_anualizado(series.precos[t]) for t in tickers], dtype=np.float64)


async def otimizar(
    db: AsyncSession,
    user_id: uuid.UUID,
    pedido: OptimizationRequest,
    *,
    taxa_livre_risco: float,
) -> OptimizationResponse:
    """Fronteira eficiente, minima variancia e maximo Sharpe.

    Quando `tickers` nao e informado, usa os ativos em carteira e devolve
    tambem a carteira atual do usuario avaliada com os mesmos parametros --
    que e o que permite ao grafico mostrar "voce esta aqui, a fronteira esta ali".
    """
    da_carteira = pedido.tickers is None
    posicoes = await transaction_service.posicoes(db, user_id)
    abertas = [p for p in posicoes if not p.esta_zerada]

    pedidos = (
        sorted({t.strip().upper() for t in pedido.tickers if t.strip()})
        if pedido.tickers is not None
        else sorted(p.ticker for p in abertas)
    )

    def vazio(motivo: str) -> OptimizationResponse:
        return OptimizationResponse(
            inicio=None,
            fim=None,
            pregoes=0,
            taxa_livre_risco=taxa_livre_risco,
            peso_maximo=pedido.peso_maximo,
            tickers=[],
            fronteira=[],
            minima_variancia=None,
            maximo_sharpe=None,
            carteira_atual=None,
            sem_historico_suficiente=pedidos,
            motivo=motivo,
        )

    if len(pedidos) < MINIMO_ATIVOS:
        return vazio(
            "A otimizacao precisa de pelo menos dois ativos. Com um so, a resposta "
            "seria '100% nele' -- correta e inutil."
        )

    # Verificacao antecipada da restricao, para explicar em vez de so falhar.
    # N ativos com teto de p cada somam no maximo N x p; se isso for menor que
    # 100%, nao existe carteira valida.
    if len(pedidos) * pedido.peso_maximo < 1.0 - 1e-9:
        minimo = 1.0 / len(pedidos)
        return vazio(
            f"{len(pedidos)} ativos com limite de {pedido.peso_maximo:.0%} cada somam "
            f"no maximo {len(pedidos) * pedido.peso_maximo:.0%} -- impossivel investir "
            f"100%. Aumente o limite para pelo menos {minimo:.0%} ou inclua mais ativos."
        )

    series = await series_service.carregar_series(db, pedidos, desde=pedido.desde, ate=pedido.ate)
    if series is None or len(series) <= MINIMO_OBSERVACOES:
        return vazio(
            "Nao ha historico de precos suficiente em comum entre estes ativos. "
            f"Sao necessarios mais de {MINIMO_OBSERVACOES} pregoes com todos negociando."
        )

    aptos = [t for t in series.tickers if len(series.precos[t]) > MINIMO_OBSERVACOES]
    if len(aptos) < MINIMO_ATIVOS:
        return vazio("Menos de dois ativos tem historico suficiente para entrar no calculo.")

    # `subconjunto` preserva o alinhamento -- remontar o dicionario a mao seria a
    # brecha por onde series desalinhadas voltariam a virar covariancia errada.
    usadas = series.subconjunto(aptos)
    tickers, cov = matriz_covariancia(usadas)
    mu = _retornos_esperados(usadas, tickers)

    try:
        min_var = carteira_para(
            minima_variancia(cov, pedido.peso_maximo), mu, cov, taxa_livre_risco
        )
        max_sharpe = carteira_para(
            maximo_sharpe(mu, cov, taxa_livre_risco, pedido.peso_maximo), mu, cov, taxa_livre_risco
        )
        fronteira = fronteira_eficiente(
            mu, cov, taxa_livre_risco, pontos=pedido.pontos, peso_maximo=pedido.peso_maximo
        )
    except OtimizacaoInviavelError as exc:
        # Rede de seguranca: a checagem antecipada acima cobre o caso previsivel,
        # mas o solver pode nao convergir por outros motivos. A restricao veio do
        # usuario, entao a resposta e vazia e explicita, nunca um 500.
        return vazio(f"Nao foi possivel otimizar com estas restricoes. {exc}")

    atual = None
    if da_carteira:
        pesos_atuais = _pesos_da_carteira(abertas, tickers)
        if pesos_atuais is not None:
            atual = _sugerida(carteira_para(pesos_atuais, mu, cov, taxa_livre_risco), tickers)

    return OptimizationResponse(
        inicio=usadas.inicio,
        fim=usadas.fim,
        pregoes=len(usadas),
        taxa_livre_risco=taxa_livre_risco,
        peso_maximo=pedido.peso_maximo,
        tickers=tickers,
        fronteira=[_sugerida(c, tickers) for c in fronteira],
        minima_variancia=_sugerida(min_var, tickers),
        maximo_sharpe=_sugerida(max_sharpe, tickers),
        carteira_atual=atual,
        sem_historico_suficiente=[t for t in pedidos if t not in tickers],
    )


def _pesos_da_carteira(posicoes: Sequence[Posicao], tickers: list[str]) -> np.ndarray | None:
    """Pesos atuais pelo CUSTO das posicoes.

    Custo, e nao valor de mercado, deliberadamente: usar valor de mercado
    exigiria buscar cotacao aqui, acoplando a otimizacao a disponibilidade de um
    fornecedor externo. O custo ja esta no banco, sempre existe, e para "quanto
    do meu capital esta em cada ativo" e uma leitura defensavel.
    """
    por_ticker = {p.ticker: float(p.custo_total) for p in posicoes}
    valores = np.array([por_ticker.get(t, 0.0) for t in tickers], dtype=np.float64)
    total = valores.sum()
    if total <= 0:
        return None
    return valores / total
