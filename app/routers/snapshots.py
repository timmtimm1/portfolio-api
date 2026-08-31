"""Rotas de snapshot: leitura pelo usuario, escrita pelo processo automatizado."""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.core.deps import CurrentUser, DbDep, ProvedorDep, SettingsDep
from app.core.rate_limit import limiter
from app.core.service_auth import ChaveDeServico
from app.schemas.snapshot import SnapshotRead, SnapshotRunResult
from app.services import snapshot_service

router = APIRouter(tags=["snapshots"])

# ~2 anos de pregoes. Serve a qualquer grafico de evolucao sem virar despejo.
LIMITE_MAXIMO = 500


@router.get(
    "/portfolio/snapshots",
    response_model=list[SnapshotRead],
    summary="Evolucao historica da carteira",
)
async def historico(
    usuario: CurrentUser,
    db: DbDep,
    desde: Annotated[date_type | None, Query()] = None,
    ate: Annotated[date_type | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = 90,
) -> list[SnapshotRead]:
    """Serie diaria de custo, valor de mercado e resultado.

    E o unico dado do sistema que NAO e reconstruivel: o valor de mercado de
    ontem dependia da cotacao de ontem, que ja foi sobrescrita no cache.
    """
    pontos = await snapshot_service.historico(db, usuario.id, desde=desde, ate=ate, limit=limit)
    return [SnapshotRead.model_validate(p) for p in pontos]


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
