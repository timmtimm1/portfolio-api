"""Rotas de gestao das carteiras."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.portfolio_schema import PortfolioCreate, PortfolioRead
from app.services import portfolio_crud
from app.services.portfolio_crud import LimiteDeCarteirasError, NomeDeCarteiraEmUsoError

router = APIRouter(prefix="/portfolios", tags=["carteiras"])


@router.get("", response_model=list[PortfolioRead], summary="Lista suas carteiras")
async def listar(usuario: CurrentUser, db: DbDep) -> list[PortfolioRead]:
    """A real primeiro, depois as simuladas por nome.

    Ordem estavel de proposito: o seletor da interface nao pode trocar de ordem
    entre dois carregamentos.
    """
    # Garante a carteira real antes de listar. Usuario novo ja a recebe -- sem
    # isso ele precisaria criar uma antes de conseguir lancar a primeira compra.
    await portfolio_crud.obter_padrao(db, usuario.id)
    carteiras = await portfolio_crud.listar(db, usuario.id)
    return [PortfolioRead.model_validate(c) for c in carteiras]


@router.post(
    "",
    response_model=PortfolioRead,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma carteira simulada",
)
async def criar(usuario: CurrentUser, db: DbDep, dados: PortfolioCreate) -> PortfolioRead:
    """Cria uma carteira, normalmente simulada.

    Simulada usa exatamente a mesma matematica da real -- mesmo preco medio,
    mesma cotacao, mesma fronteira eficiente. O tipo existe para que a interface
    nunca confunda "o que eu tenho" com "o que estou avaliando".
    """
    try:
        carteira = await portfolio_crud.criar(db, usuario.id, dados)
    except NomeDeCarteiraEmUsoError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Voce ja tem uma carteira chamada '{dados.nome}'",
        ) from None
    except LimiteDeCarteirasError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Limite de {exc} carteiras por usuario atingido",
        ) from None
    return PortfolioRead.model_validate(carteira)


@router.delete(
    "/{portfolio_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Apaga uma carteira e tudo que ha nela",
)
async def remover(usuario: CurrentUser, db: DbDep, portfolio_id: uuid.UUID) -> None:
    """Exclusao destrutiva: leva junto transacoes e historico, por cascade.

    Aqui nao ha `is_active` como em `users`. Marcar uma carteira como inativa so
    adiaria a decisao e encheria o seletor de itens que a pessoa ja descartou --
    o valor de uma simulacao abandonada e zero.

    404 para carteira de outro usuario, nunca 403.
    """
    if not await portfolio_crud.remover(db, usuario.id, portfolio_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Carteira nao encontrada")
