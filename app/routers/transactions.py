"""Rotas do livro de transacoes e da posicao consolidada."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.core.deps import CarteiraAtual, DbDep, ProvedorDep, SemDemo, SettingsDep, YahooDep
from app.core.rate_limit import limiter
from app.models.dividend import TipoProvento
from app.models.transaction import TransactionSide
from app.schemas.common import LIMITE_MAXIMO, LIMITE_PADRAO, Page
from app.schemas.dividend import (
    DividendRead,
    DividendReclassify,
    DividendSummary,
    DividendSyncResult,
)
from app.schemas.portfolio import PortfolioSummary
from app.schemas.transaction import PositionRead, TransactionCreate, TransactionRead
from app.services import (
    asset_service,
    dividend,
    dividend_service,
    portfolio_service,
    snapshot_service,
    split_service,
    transaction_service,
)
from app.services.position import VendaSemPosicaoError
from app.services.transaction_service import AtivoNaoEncontradoError

router = APIRouter(tags=["carteira"])

# 404, nao 403, para recurso de outro usuario.
#
# Responder 403 ("existe, mas nao e seu") confirma que aquele id existe -- e
# enumeracao de recursos alheios. 404 nao distingue "nao existe" de "nao e seu",
# que e exatamente a ambiguidade desejada.
_NAO_ENCONTRADA = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Transacao nao encontrada"
)


@router.post(
    "/transactions",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Registra uma compra ou venda",
)
async def criar_transacao(
    carteira: CarteiraAtual, db: DbDep, dados: TransactionCreate
) -> TransactionRead:
    """Lanca uma operacao no livro.

    O `user_id` vem do token, nunca do corpo da requisicao. Aceitar um `user_id`
    enviado pelo cliente deixaria qualquer um lancar transacoes na carteira de
    outra pessoa -- e a falha de autorizacao mais direta que existe.
    """
    try:
        transacao = await transaction_service.criar(db, carteira, dados)
    except AtivoNaoEncontradoError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Ativo {dados.ticker} nao existe no catalogo",
        ) from None
    except VendaSemPosicaoError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    # O historico a partir da data da operacao passou a estar errado: ele foi
    # calculado sem esta transacao. Refazer aqui mantem o grafico de evolucao
    # coerente com o livro, em vez de "travado" no estado anterior.
    await snapshot_service.reconstruir_desde(db, carteira, dados.traded_at)

    return TransactionRead.model_validate(transacao)


@router.get("/transactions", response_model=Page[TransactionRead], summary="Lista as operacoes")
async def listar_transacoes(
    carteira: CarteiraAtual,
    db: DbDep,
    ticker: Annotated[str | None, Query(max_length=12)] = None,
    side: Annotated[TransactionSide | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = LIMITE_PADRAO,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[TransactionRead]:
    """Extrato paginado, do mais recente para o mais antigo."""
    itens, total = await transaction_service.listar(
        db, carteira.id, ticker=ticker, side=side, limit=limit, offset=offset
    )
    return Page[TransactionRead](
        items=[TransactionRead.model_validate(t) for t in itens],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/transactions/{transacao_id}",
    response_model=TransactionRead,
    summary="Detalhe de uma operacao",
)
async def obter_transacao(
    carteira: CarteiraAtual, db: DbDep, transacao_id: uuid.UUID
) -> TransactionRead:
    transacao = await transaction_service.obter(db, carteira.id, transacao_id)
    if transacao is None:
        raise _NAO_ENCONTRADA
    return TransactionRead.model_validate(transacao)


@router.delete(
    "/transactions/{transacao_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove uma operacao",
)
async def remover_transacao(carteira: CarteiraAtual, db: DbDep, transacao_id: uuid.UUID) -> None:
    """Remove e revalida o livro.

    Apagar uma compra antiga pode invalidar vendas posteriores que dependiam
    dela; nesse caso a remocao e recusada e o livro fica intacto.
    """
    existente = await transaction_service.obter(db, carteira.id, transacao_id)
    if existente is None:
        raise _NAO_ENCONTRADA
    dia = existente.traded_at

    try:
        removida = await transaction_service.remover(db, carteira.id, transacao_id)
    except VendaSemPosicaoError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Remover esta compra deixaria o livro inconsistente. {exc}",
        ) from None
    if not removida:
        raise _NAO_ENCONTRADA

    # Sem isto, o grafico continuaria mostrando um patrimonio que nao
    # corresponde a nenhuma operacao do livro.
    await snapshot_service.reconstruir_desde(db, carteira, dia)


@router.get(
    "/portfolio/positions",
    response_model=list[PositionRead],
    summary="Posicao consolidada da carteira",
)
async def listar_posicoes(carteira: CarteiraAtual, db: DbDep) -> list[PositionRead]:
    """Quantidade, preco medio e resultado realizado por ativo.

    Nada disso e coluna no banco: tudo e reconstruido do livro a cada consulta.
    Uma unica fonte da verdade significa que o extrato sempre explica o saldo.

    Posicoes zeradas continuam aparecendo: o resultado realizado delas e
    justamente o que interessa para apuracao de imposto.
    """
    return [
        PositionRead(
            ticker=p.ticker,
            quantidade=p.quantidade,
            preco_medio=p.preco_medio,
            custo_total=p.custo_total,
            resultado_realizado=p.resultado_realizado,
        )
        for p in await transaction_service.posicoes(db, carteira.id)
    ]


@router.get(
    "/portfolio/summary",
    response_model=PortfolioSummary,
    summary="Carteira com valor de mercado e rentabilidade",
)
async def resumo_da_carteira(
    carteira: CarteiraAtual, db: DbDep, provedor: ProvedorDep, settings: SettingsDep
) -> PortfolioSummary:
    """Posicao consolidada com cotacao atual, valor de mercado e resultado.

    A cotacao vem do cache sempre que estiver dentro do TTL. Chamar a API externa
    a cada request seria o erro classico: o endpoint passa a depender da
    latencia e da disponibilidade de um terceiro, e a cota gratuita (15 mil
    chamadas/mes) evapora com poucos usuarios.

    Quando nenhum fornecedor responde, a carteira ainda e devolvida -- com custo
    e quantidade, e os tickers afetados listados em `sem_cotacao`. Degradar e
    melhor que falhar.
    """
    return await portfolio_service.resumo(
        db, provedor, carteira.id, ttl_segundos=settings.QUOTE_TTL_SECONDS
    )


@router.get(
    "/portfolio/dividends",
    response_model=DividendSummary,
    summary="Proventos recebidos pela carteira",
)
async def listar_proventos(
    carteira: CarteiraAtual,
    db: DbDep,
    desde: Annotated[date_type | None, Query()] = None,
    ate: Annotated[date_type | None, Query()] = None,
) -> DividendSummary:
    """Dividendos, JCP e rendimentos que ESTA carteira recebeu.

    Nada aqui e coluna no banco. A tabela `dividends` guarda o evento de
    mercado ("a TAEE11 pagou R$ 0,60 com data-com em 17/08"), que e igual para
    todo mundo; quanto voce recebeu e o cruzamento disso com o seu livro na
    data-com. Apagar uma compra antiga corrige os proventos junto, sem
    manutencao -- pelo mesmo motivo que a posicao e reconstruida do livro.

    O valor liquido ja desconta retencao na fonte, mas so quando o tipo e
    conhecido. Provento importado do Yahoo entra como INDEFINIDO, porque o
    fornecedor nao distingue dividendo de JCP -- e a diferenca vale 15%.
    `sem_classificacao` diz quantos estao nesse estado, para a interface poder
    avisar em vez de exibir um numero com falsa precisao.
    """
    recebidos = await dividend_service.da_carteira(db, carteira.id, desde=desde, ate=ate)

    posicoes = await transaction_service.posicoes(db, carteira.id)
    custo = sum((p.custo_total for p in posicoes), Decimal(0))

    return DividendSummary(
        total_liquido=dividend.total_liquido(recebidos),
        total_bruto=sum((r.valor_bruto for r in recebidos), Decimal(0)),
        imposto_retido=sum((r.imposto_retido for r in recebidos), Decimal(0)),
        yield_on_cost=dividend.yield_on_cost(recebidos, custo),
        sem_classificacao=sum(1 for r in recebidos if r.tipo is TipoProvento.INDEFINIDO),
        proventos=[
            DividendRead(
                ticker=r.ticker,
                data_com=r.data_com,
                tipo=r.tipo,
                quantidade=r.quantidade,
                valor_por_cota=r.valor_por_cota,
                valor_bruto=r.valor_bruto,
                valor_liquido=r.valor_liquido,
            )
            for r in recebidos
        ],
    )


@router.post(
    "/portfolio/dividends/sync",
    response_model=DividendSyncResult,
    summary="Busca proventos dos ativos da carteira no fornecedor",
    # Fora da demo: esta rota gasta cota do Yahoo, que e compartilhada.
    dependencies=[SemDemo],
)
@limiter.limit("10/hour")
async def sincronizar_proventos(
    # `Request` de verdade: o slowapi lê o IP dele, e o FastAPI trataria
    # qualquer outro tipo como parâmetro de query.
    request: Request,
    carteira: CarteiraAtual,
    db: DbDep,
    yahoo: YahooDep,
    desde: Annotated[date_type | None, Query()] = None,
) -> DividendSyncResult:
    """Consulta o Yahoo e grava os proventos que ainda não estão no banco.

    Rate limit de 10/hora porque isto faz **uma requisição externa por ativo**
    -- o endpoint de eventos do Yahoo não aceita lote. Sem o limite, recarregar
    a página em sequência viraria dezenas de chamadas a um fornecedor gratuito
    e sem contrato de serviço.

    É idempotente: gravar usa `ON CONFLICT DO NOTHING`, então rodar de novo
    devolve `gravados: 0`. E o `DO NOTHING` protege a correção manual -- um
    provento reclassificado como JCP não volta a INDEFINIDO na próxima
    sincronização.
    """
    posicoes = await transaction_service.posicoes(db, carteira.id)
    tickers = sorted({p.ticker for p in posicoes})
    if not tickers:
        return DividendSyncResult(tickers_consultados=[], gravados=0)

    # Um ano para trás por padrão: cobre o ciclo completo de qualquer pagador
    # sem puxar década de histórico que ninguém vai olhar.
    inicio = desde or (date_type.today() - timedelta(days=365))
    hoje = date_type.today()
    gravados = await dividend_service.sincronizar(db, yahoo, tickers, inicio, hoje)

    # Desdobramentos vao com uma janela MUITO maior que a dos proventos: um
    # evento de 2018 ainda ajusta uma compra de 2019, enquanto um provento de
    # 2018 nao interessa a quem comprou depois. Perder um desdobramento antigo
    # deixa a posicao errada para sempre; perder um provento antigo so deixa de
    # somar algo que nao era da carteira mesmo.
    desdobramentos = await split_service.sincronizar(
        db, yahoo, tickers, hoje - timedelta(days=365 * 20), hoje
    )
    return DividendSyncResult(
        tickers_consultados=tickers, gravados=gravados, desdobramentos=desdobramentos
    )


@router.post(
    "/portfolio/dividends/reclassificar",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Corrige o tipo de um provento importado como indefinido",
)
async def reclassificar_provento(
    carteira: CarteiraAtual, db: DbDep, dados: DividendReclassify
) -> None:
    """Marca um provento como dividendo, JCP ou rendimento.

    Por que isto existe: o Yahoo devolve valor e data e não diz o tipo. JCP tem
    15% retidos na fonte, então enquanto o provento for INDEFINIDO o líquido
    exibido pode estar até 15% acima do que caiu na conta.

    ## Sobre o escopo

    A tabela `dividends` é de MERCADO, não do usuário -- uma correção aqui vale
    para todo mundo, como corrigir um fechamento errado valeria. Exigir uma
    carteira mesmo assim é deliberado: só quem tem o ativo tem motivo (e
    contexto) para reclassificar, e isso mantém a rota fora do alcance de uma
    conta recém-criada.
    """
    posicoes = await transaction_service.posicoes(db, carteira.id)
    if dados.ticker not in {p.ticker for p in posicoes}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ativo nao esta nesta carteira",
        )

    ativo = await asset_service.buscar_por_ticker(db, dados.ticker)
    if ativo is None or not await dividend_service.reclassificar(
        db, ativo.id, dados.data_com, dados.tipo
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provento indefinido nao encontrado nesta data",
        )
