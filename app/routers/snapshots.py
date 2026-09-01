"""Rotas de snapshot: leitura pelo usuario, escrita pelo processo automatizado."""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.core.deps import BcbDep, CarteiraAtual, DbDep, IbovDep, ProvedorDep, SettingsDep
from app.core.rate_limit import limiter
from app.core.service_auth import ChaveDeServico
from app.models.benchmark import Indexador
from app.schemas.evolution import (
    BenchmarkPoint,
    BenchmarkSerie,
    Comparacao,
    EvolutionResponse,
    RentabilidadePoint,
)
from app.schemas.snapshot import SnapshotRead, SnapshotRunResult
from app.services import benchmark_service, dividend_service, snapshot_service

router = APIRouter(tags=["snapshots"])

# ~2 anos de pregoes. Serve a qualquer grafico de evolucao sem virar despejo.
LIMITE_MAXIMO = 500


@router.get(
    "/portfolio/snapshots",
    response_model=list[SnapshotRead],
    summary="Evolucao historica da carteira",
)
async def historico(
    carteira: CarteiraAtual,
    db: DbDep,
    desde: Annotated[date_type | None, Query()] = None,
    ate: Annotated[date_type | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = 90,
) -> list[SnapshotRead]:
    """Serie diaria de custo, valor de mercado e resultado.

    E o unico dado do sistema que NAO e reconstruivel: o valor de mercado de
    ontem dependia da cotacao de ontem, que ja foi sobrescrita no cache.
    """
    pontos = await snapshot_service.historico(db, carteira.id, desde=desde, ate=ate, limit=limit)
    return [SnapshotRead.model_validate(p) for p in pontos]


@router.get(
    "/portfolio/evolution",
    response_model=EvolutionResponse,
    summary="Evolucao da carteira, opcionalmente comparada a CDI, Selic, IPCA ou Ibovespa",
)
async def evolucao(
    carteira: CarteiraAtual,
    db: DbDep,
    bcb: BcbDep,
    ibov: IbovDep,
    indexador: Annotated[
        Indexador | None,
        Query(description="cdi, selic, ipca ou ibov; omita para nao comparar com nada"),
    ] = None,
    desde: Annotated[date_type | None, Query()] = None,
    ate: Annotated[date_type | None, Query()] = None,
    limit: Annotated[int, Query(ge=2, le=LIMITE_MAXIMO)] = 250,
) -> EvolutionResponse:
    """A carteira contra um indexador, respondendo "eu bati o CDI?" ou, com
    `indexador=ipca`, "meu dinheiro cresceu de verdade ou so acompanhou os
    precos?" -- a mesma pergunta de rentabilidade REAL, sem precisar de um
    campo novo: `comparacao.excesso_pontos_percentuais` contra o IPCA JA E o
    ganho acima da inflacao no periodo.

    A comparacao e OPT-IN. O default e `None` -- sem indexador, sem curva -- e
    nao `CDI`, porque um default de valor tornaria "sem comparacao" impossivel
    de pedir: omitir o parametro significaria "use o default", e o cliente nao
    teria como dizer "nenhum". O painel manda `indexador=cdi` explicitamente
    quando quer o CDI, entao a tela abre igual.

    A curva do indexador nao e a taxa acumulada pura: e quanto o MESMO dinheiro,
    aplicado nos MESMOS dias, renderia no indexador. Isso importa quando ha
    aportes -- aplicar a taxa so sobre o valor inicial subestima o benchmark e
    faz a carteira parecer melhor do que foi.

    O IPCA e publicado uma vez por MES (nao por dia util, como CDI/Selic) e sai
    com defasagem -- o valor de um mes so aparece perto do dia 10 do mes
    seguinte. `BcbClient` espalha a taxa mensal em taxas diarias equivalentes
    antes de guardar; a linha do IPCA no grafico fica, por isso, sempre um mes
    "atras" da ponta da carteira -- comportamento esperado, nao bug.

    Ibovespa nao vem do Banco Central -- o SGS descontinuou essa serie em 2019.
    Vem do Yahoo Finance como a variacao percentual diaria do indice, o que
    equivale a "quanto um ETF que segue o Ibovespa 1:1 teria rendido".

    Fontes: SGS do Banco Central (series 12, 11 e 433) para CDI/Selic/IPCA, e
    Yahoo Finance para o Ibovespa. Taxa de um dia passado nao muda, entao ela e
    gravada uma vez e nunca mais buscada.
    """
    snapshots, curva, taxas, motivo = await benchmark_service.evolucao_comparada(
        db, bcb, ibov, carteira.id, indexador, desde=desde, ate=ate, limite=limit
    )
    pontos = [SnapshotRead.model_validate(s) for s in snapshots]

    # Proventos por data-com, para entrarem no retorno TOTAL. Sem isto o
    # grafico mede so valorizacao, e uma carteira de dividendo (TAEE11, ITUB4)
    # aparece cronicamente pior do que foi: na data-com o preco cai o valor do
    # provento, o snapshot registra a queda, e o dinheiro recebido nao aparece
    # em lugar nenhum.
    recebidos = await dividend_service.da_carteira(db, carteira.id)
    proventos_por_dia: dict[date_type, Decimal] = {}
    for r in recebidos:
        # Soma acumulada: dois ativos podem ter data-com no mesmo dia.
        anterior = proventos_por_dia.get(r.data_com, Decimal(0))
        proventos_por_dia[r.data_com] = anterior + r.valor_liquido

    rentabilidade = [
        RentabilidadePoint(date=p.date, carteira=p.carteira, benchmark=p.benchmark)
        for p in benchmark_service.curva_rentabilidade(snapshots, taxas, proventos_por_dia)
    ]

    # `indexador is None` e redundante em runtime (sem indexador a curva vem
    # vazia), mas nao para o mypy: sem ele, o tipo segue `Indexador | None` la
    # embaixo, onde a serie de benchmark exige um indexador de verdade.
    if indexador is None or not curva:
        return EvolutionResponse(
            pontos=pontos,
            rentabilidade=rentabilidade,
            benchmark=None,
            comparacao=None,
            motivo=motivo,
        )

    final_bench = curva[-1].valor

    # Os percentuais da comparacao vem da curva de RENTABILIDADE (TWR), nao da
    # razao entre valores finais: com aportes, a segunda mede outra coisa.
    ultimo = rentabilidade[-1]
    carteira_pct = ultimo.carteira * 100
    benchmark_pct = ultimo.benchmark * 100 if ultimo.benchmark is not None else None
    excesso = do_indexador = None
    if benchmark_pct is not None:
        excesso = carteira_pct - benchmark_pct
        # "X% do CDI" so faz sentido com o indexador rendendo: dividir por zero
        # ou por numero negativo produziria um percentual sem significado.
        if benchmark_pct > 0:
            do_indexador = carteira_pct / benchmark_pct * 100

    return EvolutionResponse(
        pontos=pontos,
        rentabilidade=rentabilidade,
        benchmark=BenchmarkSerie(
            indexador=indexador,
            nome=benchmark_service.NOMES[indexador],
            pontos=[BenchmarkPoint(date=p.date, valor=p.valor) for p in curva],
            valor_final=final_bench,
            variacao_percentual=benchmark_pct,
        ),
        comparacao=Comparacao(
            carteira_percentual=carteira_pct,
            benchmark_percentual=benchmark_pct,
            excesso_pontos_percentuais=excesso,
            percentual_do_indexador=do_indexador,
        ),
        motivo=None,
    )


@router.post(
    "/internal/snapshots/run",
    response_model=SnapshotRunResult,
    summary="Grava a foto do dia de todas as carteiras (uso interno)",
    dependencies=[ChaveDeServico],
    tags=["interno"],
)
@limiter.limit("6/hour")
async def executar(
    # O tipo precisa ser `Request` de verdade: o slowapi le o IP dele, e o
    # FastAPI trataria qualquer outro tipo como parametro de query.
    request: Request,
    db: DbDep,
    provedor: ProvedorDep,
    settings: SettingsDep,
) -> SnapshotRunResult:
    """Disparada pelo cron do GitHub Actions apos o fechamento da B3.

    ## Por que esta rota aparece na documentacao publica

    Poderia ser escondida com `include_in_schema=False`, e a tentacao e grande.
    Mas esconder um endpoint nao o protege: quem procura acha por forca bruta de
    caminhos, e o codigo do repositorio e publico. Seguranca por obscuridade cria
    a sensacao de protecao sem a protecao.

    O que de fato protege e a chave: 384 bits comparados em tempo constante, e a
    rota nem existe (404) se a chave nao estiver configurada. Deixa-la visivel e
    documentada e mais honesto -- e um revisor consegue auditar o controle.

    O rate limit de 6/hora e uma segunda camada: mesmo com a chave vazada, ela
    nao serve para forcar recalculos em massa e derrubar o servico.

    ## Idempotente por construcao

    A chave primaria (user_id, date) faz o banco garantir uma foto por dia. Rodar
    duas vezes atualiza a mesma linha com a cotacao mais recente -- e por isso o
    cron pode ter nova tentativa sem risco de duplicar historico.
    """
    return await snapshot_service.gravar_de_todos(
        db, provedor, ttl_segundos=settings.QUOTE_TTL_SECONDS
    )
