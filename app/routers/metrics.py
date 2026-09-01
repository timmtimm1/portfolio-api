"""Rotas de metricas de risco e retorno."""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Body, Query

from app.core.deps import CarteiraAtual, CurrentUser, DbDep, ProvedorDep, SettingsDep
from app.schemas.metrics import PortfolioMetrics
from app.schemas.optimization import OptimizationRequest, OptimizationResponse
from app.schemas.rebalance import (
    DesvioRead,
    OrdemRead,
    RebalanceRequest,
    RebalanceResponse,
)
from app.services import metrics_service, optimizer_service, portfolio_service, rebalance

router = APIRouter(tags=["metricas"])

# Teto de ativos por analise. A matriz de correlacao cresce com o QUADRADO do
# numero de ativos: 50 ativos sao 2.500 celulas, 500 seriam 250 mil. O limite
# protege memoria e tempo de resposta -- e nenhuma carteira de pessoa fisica
# passa disso.
MAXIMO_ATIVOS = 50


@router.get(
    "/portfolio/metrics",
    response_model=PortfolioMetrics,
    summary="Risco e retorno da carteira",
)
async def metricas_da_carteira(
    carteira: CarteiraAtual,
    db: DbDep,
    settings: SettingsDep,
    desde: Annotated[date_type | None, Query(description="Inicio da janela (AAAA-MM-DD)")] = None,
    ate: Annotated[date_type | None, Query()] = None,
) -> PortfolioMetrics:
    """Retorno, volatilidade, Sharpe, maior queda e correlacao dos ativos em carteira.

    As series sao alinhadas pela intersecao das datas antes de qualquer calculo:
    correlacionar historicos de tamanhos diferentes produz um numero com a forma
    certa e o significado errado.
    """
    return await metrics_service.metricas_da_carteira(
        db, carteira.id, taxa_livre_risco=settings.RISK_FREE_RATE, desde=desde, ate=ate
    )


@router.get(
    "/metrics",
    response_model=PortfolioMetrics,
    summary="Risco e retorno de ativos avulsos",
)
async def metricas_de_ativos(
    _: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    tickers: Annotated[
        list[str],
        Query(min_length=1, max_length=MAXIMO_ATIVOS, description="Repita o parametro por ativo"),
    ],
    desde: Annotated[date_type | None, Query()] = None,
    ate: Annotated[date_type | None, Query()] = None,
) -> PortfolioMetrics:
    """Mesma analise para ativos que o usuario nao possui -- para avaliar antes
    de comprar. E a rota que o simulador da Etapa 9 vai consumir."""
    return await metrics_service.metricas(
        db, tickers, taxa_livre_risco=settings.RISK_FREE_RATE, desde=desde, ate=ate
    )


@router.post(
    "/portfolio/optimize",
    response_model=OptimizationResponse,
    summary="Fronteira eficiente de Markowitz",
)
async def otimizar_carteira(
    carteira: CarteiraAtual,
    db: DbDep,
    settings: SettingsDep,
    pedido: Annotated[OptimizationRequest | None, Body()] = None,
) -> OptimizationResponse:
    """Calcula a fronteira eficiente, a carteira de minima variancia e a de
    maximo Sharpe para os ativos informados (ou os que voce tem em carteira).

    ## Por que POST e nao GET

    O pedido tem corpo estruturado -- lista de ativos, limite por ativo, janela.
    Espremer isso em query string produziria URLs longas e frageis, e alguns
    proxies truncam. O metodo nao e idempotente no sentido de cache, mas tambem
    nao altera nada no servidor: e uma consulta com corpo, e POST e o verbo
    pragmatico para isso.

    ## O que o resultado significa, e o que nao significa

    A fronteira e otima **para o passado observado**. O retorno esperado e
    estimado sobre o historico, e essa estimativa e a parte fraca do modelo:
    pequenas mudancas na janela produzem carteiras bem diferentes.

    A carteira de **minima variancia** e mais confiavel que a de maximo Sharpe,
    porque nao usa retorno esperado -- so covariancia, que e bem mais estavel.

    Por isso a resposta sempre inclui o campo `aviso`, e por isso existe o limite
    `peso_maximo`: sem ele, o otimizador rotineiramente aloca quase tudo no papel
    que mais subiu na amostra -- matematicamente otimo para o passado, e o oposto
    de diversificar.
    """
    # Instanciado aqui, nao no default do parametro: um objeto criado na
    # assinatura seria construido UMA vez, no import, e compartilhado por todos
    # os requests -- a armadilha classica do argumento padrao mutavel em Python.
    return await optimizer_service.otimizar(
        db, carteira.id, pedido or OptimizationRequest(), taxa_livre_risco=settings.RISK_FREE_RATE
    )


@router.post(
    "/portfolio/rebalance",
    response_model=RebalanceResponse,
    summary="Traduz pesos-alvo em ordens de compra e venda",
)
async def rebalancear(
    carteira: CarteiraAtual,
    db: DbDep,
    provedor: ProvedorDep,
    settings: SettingsDep,
    pedido: RebalanceRequest,
) -> RebalanceResponse:
    """Do peso ideal para a ordem concreta.

    A fronteira diz "40% em WEGE3". Ninguem compra 40%: compra 12 acoes. Aqui
    a traducao acontece, com a cotacao de mercado e quantidades inteiras.

    ## Os dois modos

    `permitir_venda=false` distribui apenas o aporte. Nao vende nada, logo nao
    gera imposto sobre ganho nem realiza prejuizo so para acertar um peso -- e
    como a maioria das pessoas rebalanceia de verdade.

    `permitir_venda=true` chega exatamente no alvo, vendendo o excedente. Mais
    preciso, e o plano mostra quanto sera vendido para a pessoa decidir se
    compensa o custo.

    ## Isto NAO executa nada

    Devolve um plano. Nenhuma transacao e gravada, nenhuma ordem e enviada a
    corretora -- o app nao tem (nem quer ter) essa integracao. Quem decide e
    lanca e o usuario.
    """
    resumo = await portfolio_service.resumo(
        db, provedor, carteira.id, ttl_segundos=settings.QUOTE_TTL_SECONDS
    )

    plano = rebalance.planejar(
        resumo.positions,
        pedido.pesos,
        aporte=pedido.aporte,
        permitir_venda=pedido.permitir_venda,
    )

    return RebalanceResponse(
        ordens=[
            OrdemRead(
                ticker=o.ticker,
                side=o.side,
                quantidade=o.quantidade,
                preco=o.preco,
                valor=o.valor,
            )
            for o in plano.ordens
        ],
        desvios=[
            DesvioRead(
                ticker=d.ticker,
                peso_atual=d.peso_atual,
                peso_alvo=d.peso_alvo,
                diferenca=d.diferenca,
                valor_atual=d.valor_atual,
            )
            # Sem posicao e sem alvo nao ha desvio nenhum a mostrar.
            for d in plano.desvios
            if d.valor_atual > 0 or d.peso_alvo > 0
        ],
        total_compras=plano.total_compras,
        total_vendas=plano.total_vendas,
        sobra=plano.sobra,
        sem_preco=plano.sem_preco,
    )
