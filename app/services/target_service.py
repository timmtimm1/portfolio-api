"""Persistencia do alvo de preco (stop gain / stop loss) por ativo.

Camada fina sobre `app/services/target.py`, que faz a conta. Aqui so mora
o que toca o banco: upsert, remocao, e a leitura de todos os alvos de uma
carteira para casar com as posicoes no resumo de mercado.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.portfolio import Portfolio
from app.models.target import AssetTarget, TipoAlvo
from app.schemas.target import AlvoResumo, MetaResumo, TradePlano
from app.services import target, trade


async def definir(
    db: AsyncSession,
    carteira: Portfolio,
    asset_id: uuid.UUID,
    *,
    stop_gain_tipo: TipoAlvo | None,
    stop_gain_valor: Decimal | None,
    stop_loss_tipo: TipoAlvo | None,
    stop_loss_valor: Decimal | None,
    meta_valor: Decimal | None,
) -> AssetTarget:
    """Upsert do alvo inteiro, de uma vez.

    Nao existe "atualiza so o stop gain, preserva o loss que ja estava
    configurado" por fora: quem edita reenvia os dois lados, e o lado que vier
    nulo apaga o que estava la. Um PATCH parcial pareceria mais conveniente,
    mas esconderia o caso real de alguem querer remover so o stop loss -- com
    upsert total, "remover um lado" e simplesmente "reenviar nulo nele".
    """
    stmt = insert(AssetTarget).values(
        portfolio_id=carteira.id,
        asset_id=asset_id,
        user_id=carteira.user_id,
        stop_gain_tipo=stop_gain_tipo,
        stop_gain_valor=stop_gain_valor,
        stop_loss_tipo=stop_loss_tipo,
        stop_loss_valor=stop_loss_valor,
        meta_valor=meta_valor,
    )
    # `RETURNING` poupa o SELECT extra que um `db.get()` depois do upsert
    # exigiria -- e devolve o tipo certo (`AssetTarget`, nao `AssetTarget | None`)
    # sem precisar de um `assert` para provar o que o UPSERT ja garantiu.
    #
    # A cadeia inteira fica numa unica expressao, nao reatribuida a `stmt`:
    # `.on_conflict_do_update(...).returning(...)` muda o tipo estatico do
    # statement, e o mypy acusa incompatibilidade se essa mudanca de tipo for
    # atribuida de volta a uma variavel que comecou como `Insert` simples.
    alvo = (
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[AssetTarget.portfolio_id, AssetTarget.asset_id],
                set_={
                    "stop_gain_tipo": stmt.excluded.stop_gain_tipo,
                    "stop_gain_valor": stmt.excluded.stop_gain_valor,
                    "stop_loss_tipo": stmt.excluded.stop_loss_tipo,
                    "stop_loss_valor": stmt.excluded.stop_loss_valor,
                    "meta_valor": stmt.excluded.meta_valor,
                },
            ).returning(AssetTarget)
        )
    ).scalar_one()
    await db.commit()
    return alvo


async def remover(db: AsyncSession, carteira: Portfolio, asset_id: uuid.UUID) -> bool:
    """Apaga o alvo. Devolve se havia algo para apagar."""
    # `CursorResult` expoe rowcount; o `Result` generico do stub nao. O cast
    # documenta que um DELETE sempre devolve o primeiro (mesmo padrao de
    # `transaction_service.remover_todas`).
    resultado = cast(
        CursorResult[Any],
        await db.execute(
            delete(AssetTarget).where(
                AssetTarget.portfolio_id == carteira.id, AssetTarget.asset_id == asset_id
            )
        ),
    )
    await db.commit()
    return bool(resultado.rowcount)


async def dos_ativos(db: AsyncSession, portfolio_id: uuid.UUID) -> dict[str, AssetTarget]:
    """Todos os alvos da carteira, indexados por TICKER.

    Por ticker, e nao por `asset_id`: `portfolio_service.resumo()` trabalha
    com `Posicao.ticker` (o calculo de posicao e feito em cima do livro, que
    nao carrega o `asset_id`) -- indexar aqui do mesmo jeito evita uma segunda
    tabela de tradução no meio do caminho.
    """
    linhas = (
        await db.execute(
            select(Asset.ticker, AssetTarget)
            .join(AssetTarget, AssetTarget.asset_id == Asset.id)
            .where(AssetTarget.portfolio_id == portfolio_id)
        )
    ).all()
    return {ticker: alvo for ticker, alvo in linhas}


def resumo_de(
    alvo: AssetTarget | None,
    *,
    preco_medio: Decimal,
    preco_atual: Decimal | None,
    valor_atual: Decimal = Decimal(0),
    quantidade: Decimal = Decimal(0),
) -> AlvoResumo:
    """Traduz o model (ou a ausencia dele) para o schema exposto pela API.

    Ponto unico -- usado tanto pelo resumo da carteira inteira quanto pela
    resposta imediata de `PUT /portfolio/targets/{ticker}` -- para as duas
    rotas nunca correrem o risco de calcular o status de jeitos diferentes.
    """
    status = target.avaliar(alvo, preco_medio=preco_medio, preco_atual=preco_atual)
    if alvo is None:
        return AlvoResumo(status=status)

    progresso = target.progresso_da_meta(valor_atual, alvo.meta_valor)
    return AlvoResumo(
        stop_gain_tipo=alvo.stop_gain_tipo,
        stop_gain_valor=alvo.stop_gain_valor,
        stop_loss_tipo=alvo.stop_loss_tipo,
        stop_loss_valor=alvo.stop_loss_valor,
        status=status,
        meta=_meta_resumo(progresso),
        fixado_no_trade=alvo.fixado_no_trade,
        trade=_trade_plano(quantidade, preco_medio, preco_atual),
    )


def _trade_plano(
    quantidade: Decimal, preco_medio: Decimal, preco_atual: Decimal | None
) -> TradePlano | None:
    """O plano PADRAO: recuperar o custo, sem lucro extra.

    O "e se eu quisesse tirar R$ 500?" e interativo e roda na tela; aqui fica
    o caso de partida, que e o que a area de trade mostra sem ninguem digitar
    nada.
    """
    if preco_atual is None:
        return None
    plano = trade.planejar(quantidade=quantidade, preco_medio=preco_medio, preco_atual=preco_atual)
    if plano is None:
        return None
    return TradePlano(
        vender=plano.vender,
        residuo=plano.residuo,
        recebe=plano.recebe,
        custo_recuperado=plano.custo_recuperado,
        sobra_em_caixa=plano.sobra_em_caixa,
        residuo_valor=plano.residuo_valor,
        viavel=plano.viavel,
    )


async def fixar_no_trade(
    db: AsyncSession, carteira: Portfolio, asset_id: uuid.UUID, *, fixado: bool
) -> None:
    """Marca (ou solta) o papel na area de trade.

    Upsert, e nao UPDATE: fixar um papel que nunca teve alvo nenhum e o caso
    comum -- "vou vender esta" nao pressupoe ter configurado stop antes.
    """
    stmt = insert(AssetTarget).values(
        portfolio_id=carteira.id,
        asset_id=asset_id,
        user_id=carteira.user_id,
        fixado_no_trade=fixado,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AssetTarget.portfolio_id, AssetTarget.asset_id],
            set_={"fixado_no_trade": stmt.excluded.fixado_no_trade},
        )
    )
    await db.commit()


def _meta_resumo(progresso: target.ProgressoDaMeta | None) -> MetaResumo | None:
    if progresso is None:
        return None
    return MetaResumo(
        meta=progresso.meta,
        atual=progresso.atual,
        falta=progresso.falta,
        progresso=progresso.progresso,
        atingida=progresso.atingida,
    )
