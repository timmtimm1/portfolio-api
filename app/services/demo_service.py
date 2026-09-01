"""Conta de demonstracao: descartavel, populada e com validade.

## Por que uma CONTA, e nao um caminho anonimo

Todo o isolamento deste app vive em `get_current_user` e `get_carteira`. Cada
consulta passa por ali, e e isso que garante que ninguem veja a carteira de
outro.

Um modo "sem login" seria uma SEGUNDA porta para a camada de dados -- sem
nenhuma dessas garantias, e sem os testes que ja protegem a primeira. Num banco
onde a carteira real do dono convive com a demonstracao, esse e exatamente o
atalho que vira vazamento.

Sendo um usuario comum com senha inutilizavel, nao ha caminho novo para
revisar: o escopo que ja existe passa a proteger a demo de graca.

Isso resolve tambem o problema pratico de dois visitantes ao mesmo tempo. Com
uma conta compartilhada, um lanca uma operacao e o outro ve aparecer na tela.

## Por que ela nasce populada

Carteira vazia mostra tela vazia. O visitante veio de um link para ver a
fronteira eficiente funcionando, e "cadastre uma operacao para comecar" nao
demonstra nada. Ela nasce com cinco ativos e alguns meses de operacoes, e com
os snapshots ja reconstruidos -- entao grafico, correlacao, fronteira e
projecao aparecem prontos no primeiro segundo.

E continua editavel: quem quiser lancar, apagar e simular por cima, pode.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.asset import Asset
from app.models.portfolio import Portfolio, TipoCarteira
from app.models.transaction import Transaction, TransactionSide
from app.models.user import User
from app.services import snapshot_service

logger = logging.getLogger(__name__)

NOME_DA_CARTEIRA = "Carteira de demonstracao"

# A carteira semeada. Tickers com historico de preco na base -- sem serie, a
# fronteira e as metricas nao teriam o que calcular e a demonstracao mostraria
# telas vazias, que e justamente o que ela existe para evitar.
#
# `dias_atras` espalha as compras ao longo de meses: uma carteira com todas as
# operacoes no mesmo dia produz um grafico de evolucao com dois pontos.
SEMENTE: tuple[tuple[str, int, str, int], ...] = (
    # ticker, quantidade, preco, dias atras
    ("WEGE3", 100, "37.45", 107),
    ("ITUB4", 100, "32.45", 102),
    ("PETR4", 20, "41.00", 12),
    ("VALE3", 10, "78.00", 12),
    ("TAEE11", 20, "37.39", 12),
)


def _email_descartavel() -> str:
    """Email unico e obviamente sintetico.

    `example.com` e reservado pela RFC 2606 para documentacao: nunca vai
    receber mensagem de verdade, entao nao ha risco de colidir com o email de
    alguem nem de uma mensagem escapar para um destinatario real.

    A primeira versao usava `.invalid` -- que e o dominio reservado ainda mais
    correto para "isto nao existe". Mas o `EmailStr` do Pydantic recusa TLDs de
    uso especial, e como o `UserRead` valida na SAIDA, todo `/auth/me` de conta
    demo respondia 500. Semanticamente melhor, praticamente quebrado.
    """
    return f"demo-{secrets.token_hex(8)}@example.com"


async def criar(db: AsyncSession, *, validade_horas: int) -> User:
    """Cria a conta, popula a carteira e reconstroi o historico.

    A senha e um valor aleatorio que ninguem conhece -- nem quem cria. A conta
    so e acessivel pelos tokens devolvidos na hora; nao ha login possivel nela,
    o que fecha a porta de alguem tentar entrar numa demo alheia.
    """
    usuario = User(
        email=_email_descartavel(),
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        is_demo=True,
        expires_at=datetime.now(UTC) + timedelta(hours=validade_horas),
    )
    db.add(usuario)
    await db.flush()

    # SIMULADA, nunca real. O tipo governa a cor e o rotulo na tela, e confundir
    # demonstracao com posicao de verdade e o erro que este projeto inteiro nao
    # pode cometer.
    carteira = Portfolio(user_id=usuario.id, nome=NOME_DA_CARTEIRA, tipo=TipoCarteira.SIMULADA)
    db.add(carteira)
    await db.flush()

    await _semear(db, usuario, carteira)
    await db.commit()

    # Sem snapshots o grafico de evolucao nasce vazio. O backfill os reconstroi
    # a partir de `price_history`, entao a demo ja abre com a curva desenhada.
    try:
        await snapshot_service.backfill(
            db, carteira, desde=datetime.now(UTC).date() - timedelta(days=180)
        )
    except Exception:  # noqa: BLE001
        # Historico e um extra: sem ele a demo abre com o grafico vazio, mas
        # abre. Deixar a criacao da conta falhar por causa disso trocaria uma
        # tela incompleta por uma porta que nao abre.
        logger.warning("[demo] falha ao reconstruir historico", exc_info=True)

    return usuario


async def _semear(db: AsyncSession, usuario: User, carteira: Portfolio) -> None:
    """Lanca as operacoes da semente, pulando ticker que nao existe no catalogo.

    Pular em vez de estourar: a base de um deploy novo pode nao ter todos os
    papeis, e uma demo com quatro ativos e melhor que uma demo que nao abre.
    """
    tickers = [t for t, *_ in SEMENTE]
    ids = {
        ticker: asset_id
        for ticker, asset_id in (
            await db.execute(select(Asset.ticker, Asset.id).where(Asset.ticker.in_(tickers)))
        ).all()
    }

    hoje = datetime.now(UTC).date()
    for ticker, quantidade, preco, dias in SEMENTE:
        asset_id = ids.get(ticker)
        if asset_id is None:
            logger.info("[demo] %s nao esta no catalogo; pulando", ticker)
            continue
        db.add(
            Transaction(
                user_id=usuario.id,
                portfolio_id=carteira.id,
                asset_id=asset_id,
                side=TransactionSide.COMPRA,
                quantity=Decimal(quantidade),
                price=Decimal(preco),
                fees=Decimal(0),
                traded_at=hoje - timedelta(days=dias),
            )
        )


async def limpar_expiradas(db: AsyncSession) -> int:
    """Apaga as contas demo vencidas. Devolve quantas sairam.

    A exclusao leva junto carteiras, transacoes, snapshots e refresh tokens por
    `ON DELETE CASCADE` -- o cascade do BANCO, nao o do ORM. A distincao e o que
    faz este `delete()` em massa funcionar: ele nao carrega objeto nenhum, entao
    um cascade declarado so no `relationship` seria simplesmente ignorado e as
    linhas filhas ficariam orfas. Quem mexer nessas foreign keys mexe nisto.

    Apagar a linha e o certo AQUI, e so aqui. Na conta de verdade o mesmo
    cascade destruiria o historico que o usuario mais quer preservar -- por isso
    la a regra e desativar, nunca deletar.

    Roda na criacao da proxima demo, e nao num cron: a faxina acontece
    exatamente quando o volume cresce, sem depender de agendador configurado.
    Se ninguem visita, nao ha o que limpar.
    """
    vencidas = (
        (
            await db.execute(
                select(User).where(User.is_demo.is_(True), User.expires_at < datetime.now(UTC))
            )
        )
        .scalars()
        .all()
    )
    if not vencidas:
        return 0

    ids: list[uuid.UUID] = [u.id for u in vencidas]
    await db.execute(delete(User).where(User.id.in_(ids)))
    await db.commit()
    logger.info("[demo] %d conta(s) expirada(s) removida(s)", len(ids))
    return len(ids)
