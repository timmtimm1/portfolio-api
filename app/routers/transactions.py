"""Rotas do livro de transacoes e da posicao consolidada."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.core.deps import CurrentUser, DbDep, ProvedorDep, SettingsDep
from app.models.transaction import TransactionSide
from app.schemas.common import LIMITE_MAXIMO, LIMITE_PADRAO, Page
from app.schemas.portfolio import PortfolioSummary
from app.schemas.transaction import PositionRead, TransactionCreate, TransactionRead
from app.services import portfolio_service, transaction_service
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
    usuario: CurrentUser, db: DbDep, dados: TransactionCreate
) -> TransactionRead:
    """Lanca uma operacao no livro.

    O `user_id` vem do token, nunca do corpo da requisicao. Aceitar um `user_id`
    enviado pelo cliente deixaria qualquer um lancar transacoes na carteira de
    outra pessoa -- e a falha de autorizacao mais direta que existe.
    """
    try:
        transacao = await transaction_service.criar(db, usuario.id, dados)
    except AtivoNaoEncontradoError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Ativo {dados.ticker} nao existe no catalogo",
        ) from None
    except VendaSemPosicaoError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return TransactionRead.model_validate(transacao)


@router.get("/transactions", response_model=Page[TransactionRead], summary="Lista as operacoes")
async def listar_transacoes(
    usuario: CurrentUser,
    db: DbDep,
    ticker: Annotated[str | None, Query(max_length=12)] = None,
    side: Annotated[TransactionSide | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = LIMITE_PADRAO,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[TransactionRead]:
    """Extrato paginado, do mais recente para o mais antigo."""
    itens, total = await transaction_service.listar(
        db, usuario.id, ticker=ticker, side=side, limit=limit, offset=offset
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
    usuario: CurrentUser, db: DbDep, transacao_id: uuid.UUID
) -> TransactionRead:
    transacao = await transaction_service.obter(db, usuario.id, transacao_id)
    if transacao is None:
        raise _NAO_ENCONTRADA
    return TransactionRead.model_validate(transacao)


@router.delete(
    "/transactions/{transacao_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove uma operacao",
)
async def remover_transacao(usuario: CurrentUser, db: DbDep, transacao_id: uuid.UUID) -> None:
    """Remove e revalida o livro.

    Apagar uma compra antiga pode invalidar vendas posteriores que dependiam
    dela; nesse caso a remocao e recusada e o livro fica intacto.
    """
    try:
        removida = await transaction_service.remover(db, usuario.id, transacao_id)
    except VendaSemPosicaoError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Remover esta compra deixaria o livro inconsistente. {exc}",
        ) from None
    if not removida:
        raise _NAO_ENCONTRADA


@router.get(
    "/portfolio/positions",
    response_model=list[PositionRead],
    summary="Posicao consolidada da carteira",
)
async def listar_posicoes(usuario: CurrentUser, db: DbDep) -> list[PositionRead]:
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
        for p in await transaction_service.posicoes(db, usuario.id)
    ]


@router.get(
    "/portfolio/summary",
    response_model=PortfolioSummary,
    summary="Carteira com valor de mercado e rentabilidade",
)
async def resumo_da_carteira(
    usuario: CurrentUser, db: DbDep, provedor: ProvedorDep, settings: SettingsDep
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
        db, provedor, usuario.id, ttl_segundos=settings.QUOTE_TTL_SECONDS
    )
