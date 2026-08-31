"""Rotas do catalogo de ativos da B3."""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.core.deps import CurrentUser, DbDep
from app.models.asset import AssetType
from app.schemas.asset import AssetRead, PricePoint
from app.schemas.common import LIMITE_MAXIMO, LIMITE_PADRAO, Page
from app.services import asset_service

# `dependencies=[...]` no router inteiro, nao rota a rota.
#
# Autenticacao por omissao e a postura correta: se um dia alguem adicionar uma
# rota nova aqui e esquecer de protege-la, ela ja nasce protegida. O inverso --
# proteger uma a uma -- falha silenciosamente, e ninguem percebe que a rota nova
# esta aberta ate alguem notar.
router = APIRouter(prefix="/assets", tags=["ativos"])


@router.get("", response_model=Page[AssetRead], summary="Lista os ativos do catalogo")
async def listar_ativos(
    _: CurrentUser,
    db: DbDep,
    busca: Annotated[
        str | None, Query(max_length=60, description="Trecho do ticker ou do nome")
    ] = None,
    tipo: Annotated[AssetType | None, Query(description="acao, fii, etf, unit, bdr")] = None,
    setor: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = LIMITE_PADRAO,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AssetRead]:
    """Catalogo paginado.

    A paginacao e obrigatoria, nao opcional: hoje sao 151 ativos e devolver tudo
    "funcionaria". O problema e que funcionar com 151 e o que faz ninguem
    perceber o defeito ate a tabela ter 50 mil linhas -- e ai a correcao e uma
    mudanca quebrando o contrato de quem ja consome.

    Limitacao conhecida do OFFSET: `offset=100000` faz o banco varrer e descartar
    cem mil linhas antes de devolver a pagina. Para um catalogo de milhares isso
    e irrelevante; para uma listagem de milhoes, a resposta e paginacao por
    cursor (keyset), que filtra por `WHERE ticker > :ultimo` e usa o indice.
    Fica registrado por ser uma escolha, nao um esquecimento.
    """
    itens, total = await asset_service.listar(
        db, busca=busca, tipo=tipo, setor=setor, limit=limit, offset=offset
    )
    return Page[AssetRead](
        items=[AssetRead.model_validate(a) for a in itens],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{ticker}", response_model=AssetRead, summary="Detalhe de um ativo")
async def obter_ativo(
    _: CurrentUser,
    db: DbDep,
    ticker: Annotated[str, Path(max_length=12, pattern=r"^[A-Za-z0-9]{4,6}$")],
) -> AssetRead:
    """O `pattern` no path e validacao de entrada na borda: qualquer coisa fora
    do formato de ticker e recusada com 422 antes de chegar ao banco."""
    ativo = await asset_service.buscar_por_ticker(db, ticker)
    if ativo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ativo nao encontrado")
    return AssetRead.model_validate(ativo)


@router.get(
    "/{ticker}/history",
    response_model=list[PricePoint],
    summary="Historico de fechamentos",
)
async def historico_do_ativo(
    _: CurrentUser,
    db: DbDep,
    ticker: Annotated[str, Path(max_length=12, pattern=r"^[A-Za-z0-9]{4,6}$")],
    desde: Annotated[date_type | None, Query(description="Data inicial (AAAA-MM-DD)")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 252,
) -> list[PricePoint]:
    """Fechamentos mais recentes primeiro.

    O padrao de 252 nao e arbitrario: e o numero aproximado de pregoes num ano, a
    janela usual para volatilidade anualizada. O teto de 2000 (cerca de 8 anos)
    impede que um `limit` grande transforme a rota num despejo da tabela.
    """
    ativo = await asset_service.buscar_por_ticker(db, ticker)
    if ativo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ativo nao encontrado")
    pontos = await asset_service.historico(db, ativo.id, desde=desde, limit=limit)
    return [PricePoint.model_validate(p) for p in pontos]
