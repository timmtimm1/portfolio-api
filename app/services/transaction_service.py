"""Regras do livro de transacoes.

Toda funcao aqui recebe `portfolio_id` e filtra por ele. A checagem de dono
acontece uma vez, em `get_carteira` -- o unico caminho pelo qual um
`portfolio_id` entra no sistema. Daqui para dentro, carteira alheia nao existe.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.asset import Asset
from app.models.portfolio import Portfolio, TipoCarteira
from app.models.snapshot import PortfolioSnapshot
from app.models.transaction import Transaction, TransactionSide
from app.schemas.transaction import TransactionCreate
from app.services import split, split_service
from app.services.exceptions import DomainError
from app.services.position import Posicao, calcular_posicoes


class AtivoNaoEncontradoError(DomainError):
    pass


class CarteiraRealNaoZeravelError(DomainError):
    """A carteira real guarda operacoes de verdade; zerar tudo de uma vez nao e
    uma opcao para ela -- so a remocao unitaria, uma a uma."""


def _da_carteira(portfolio_id: uuid.UUID) -> Select[tuple[Transaction]]:
    """Ponto unico onde o filtro de escopo e aplicado.

    Filtra por CARTEIRA, nao por usuario: a checagem de dono ja aconteceu em
    `get_carteira`, que e o unico caminho pelo qual um `portfolio_id` entra no
    sistema. Repetir a verificacao aqui daria uma falsa sensacao de defesa em
    profundidade -- na pratica, um dia alguem chamaria estas funcoes com um id
    que nao passou pela dependencia, e a checagem duplicada nao ajudaria.

    Centralizar continua sendo controle de autorizacao, nao estilo: espalhar o
    `.where` por dez funcoes garante que a decima primeira saia sem ele.
    """
    return select(Transaction).where(Transaction.portfolio_id == portfolio_id)


async def _carregar_do_ativo(
    db: AsyncSession, portfolio_id: uuid.UUID, asset_id: uuid.UUID
) -> list[Transaction]:
    """Todas as transacoes do usuario num ativo, com o Asset ja carregado.

    `selectinload` traz os ativos numa segunda consulta em vez de uma por linha.
    Sem ele -- e com `lazy="raise"` no model -- o acesso a `.asset` levantaria
    excecao, que e justamente o objetivo: o N+1 falha alto, nao silenciosamente.
    """
    stmt = (
        _da_carteira(portfolio_id)
        .where(Transaction.asset_id == asset_id)
        .options(selectinload(Transaction.asset))
    )
    return list((await db.execute(stmt)).scalars().all())


async def criar(db: AsyncSession, carteira: Portfolio, dados: TransactionCreate) -> Transaction:
    """Registra uma operacao, validando o livro inteiro daquele ativo.

    A validacao nao olha so a posicao atual: ela recalcula o livro completo em
    ordem cronologica JA COM a nova transacao. E necessario porque uma operacao
    pode ser lancada com data retroativa -- inserir uma venda antiga no meio do
    historico pode deixar negativa uma posicao que hoje esta positiva. Conferir
    apenas o saldo de hoje deixaria passar exatamente esse caso.
    """
    ativo = (
        await db.execute(select(Asset).where(Asset.ticker == dados.ticker))
    ).scalar_one_or_none()
    if ativo is None:
        raise AtivoNaoEncontradoError(dados.ticker)

    transacao = Transaction(
        # `user_id` continua gravado, redundante com a carteira: e ele que torna
        # possivel varrer tudo de um usuario (LGPD, exclusao de conta) sem um
        # JOIN, e que sustenta o CASCADE quando a conta some.
        user_id=carteira.user_id,
        portfolio_id=carteira.id,
        asset_id=ativo.id,
        side=dados.side,
        quantity=dados.quantity,
        price=dados.price,
        fees=dados.fees,
        traded_at=dados.traded_at,
        note=dados.note,
    )

    existentes = await _carregar_do_ativo(db, carteira.id, ativo.id)
    # Objeto leve so para a validacao: nao adicionamos a transacao a sessao antes
    # de ter certeza, para nao depender de rollback para desfazer.
    novo = _Candidata(ativo.ticker, dados)
    # O livro precisa estar AJUSTADO aqui, nao so na leitura. Quem comprou 100
    # acoes antes de um desdobramento 2:1 tem 200 hoje e pode vender 150 -- com
    # o livro cru, essa venda legitima seria recusada como venda a descoberto.
    eventos = await split_service.dos_ativos(db, {ativo.id})
    calcular_posicoes(split.ajustar([*existentes, novo], eventos))

    db.add(transacao)
    await db.commit()
    await db.refresh(transacao, attribute_names=["id", "created_at", "updated_at"])
    transacao.asset = ativo
    return transacao


class _Candidata:
    """Adaptador da transacao ainda nao gravada para o Protocol do calculo."""

    def __init__(self, ticker: str, dados: TransactionCreate) -> None:
        self.ticker = ticker
        self.side: TransactionSide = dados.side
        self.quantity = dados.quantity
        self.price = dados.price
        self.fees = dados.fees
        self.traded_at = dados.traded_at


async def listar(
    db: AsyncSession,
    portfolio_id: uuid.UUID,
    *,
    ticker: str | None = None,
    side: TransactionSide | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Transaction], int]:
    stmt = _da_carteira(portfolio_id).options(selectinload(Transaction.asset)).join(Asset)
    contagem = (
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.portfolio_id == portfolio_id)
    )

    if ticker:
        stmt = stmt.where(Asset.ticker == ticker.strip().upper())
        contagem = contagem.join(Asset).where(Asset.ticker == ticker.strip().upper())
    if side is not None:
        stmt = stmt.where(Transaction.side == side)
        contagem = contagem.where(Transaction.side == side)

    total = int(await db.scalar(contagem) or 0)
    itens = (
        (
            await db.execute(
                stmt.order_by(Transaction.traded_at.desc(), Transaction.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(itens), total


async def obter(
    db: AsyncSession, portfolio_id: uuid.UUID, transacao_id: uuid.UUID
) -> Transaction | None:
    """Busca por id E por carteira, na mesma consulta.

    Buscar so por id e depois comparar em Python funciona -- ate alguem esquecer
    a comparacao. Com o filtro na consulta, a transacao de outra carteira
    simplesmente nao existe para este codigo.
    """
    stmt = (
        _da_carteira(portfolio_id)
        .where(Transaction.id == transacao_id)
        .options(selectinload(Transaction.asset))
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def remover(db: AsyncSession, portfolio_id: uuid.UUID, transacao_id: uuid.UUID) -> bool:
    """Apaga e revalida o livro do ativo afetado.

    Apagar uma COMPRA antiga pode deixar invalidas vendas posteriores que
    dependiam dela. Sem revalidar, o livro ficaria num estado que o proprio
    sistema recusaria criar -- e o calculo de posicao passaria a levantar
    excecao em toda consulta seguinte.
    """
    transacao = await obter(db, portfolio_id, transacao_id)
    if transacao is None:
        return False

    asset_id = transacao.asset_id
    await db.execute(
        delete(Transaction).where(
            Transaction.id == transacao_id, Transaction.portfolio_id == portfolio_id
        )
    )
    await db.flush()

    restantes = await _carregar_do_ativo(db, portfolio_id, asset_id)
    eventos = await split_service.dos_ativos(db, {asset_id})
    try:
        calcular_posicoes(split.ajustar(restantes, eventos))
    except DomainError:
        await db.rollback()
        raise

    await db.commit()
    return True


async def remover_todas(db: AsyncSession, carteira: Portfolio) -> int:
    """Zera o livro inteiro de uma vez. Devolve quantas operacoes saíram.

    Existe porque `remover()` so tira uma por vez -- e revalida o livro a cada
    chamada. Numa carteira simulada com meses de compra e venda, isso obriga a
    apagar de tras para frente (senao uma compra antiga com venda posterior
    recusa a remocao com 409) e um clique por linha. Para recomecar a
    simulacao do zero, "um a um" nao e uma opcao pratica.

    A REAL fica de fora, e por um motivo diferente do 409 unitario: la a
    validacao por linha e uma trava contra erro de sequencia, aqui seria
    apagar decadas de operacoes de verdade num clique so. Ledger-as-truth
    significa que a transacao E o dado -- perde-la nao e "resetar", e destruir
    o unico registro que existe.

    Os snapshots vao junto. Sem nenhuma transacao, nao ha posicao nem valor a
    fotografar -- manter os pontos antigos deixaria o grafico de evolucao
    contando a historia de uma carteira que nao existe mais.
    """
    if carteira.tipo is TipoCarteira.REAL:
        raise CarteiraRealNaoZeravelError(
            "A carteira real nao pode ser zerada de uma vez. "
            "Remova as operacoes uma a uma se precisar corrigir alguma."
        )

    # `CursorResult` expoe rowcount; o `Result` generico do stub nao. O cast
    # documenta que um DELETE sempre devolve o primeiro (mesmo padrao de
    # `snapshot_service.reconstruir_desde`).
    resultado = cast(
        CursorResult[Any],
        await db.execute(delete(Transaction).where(Transaction.portfolio_id == carteira.id)),
    )
    await db.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == carteira.id))
    await db.commit()
    return resultado.rowcount or 0


async def posicoes(db: AsyncSession, portfolio_id: uuid.UUID) -> list[Posicao]:
    """Posicoes consolidadas, reconstruidas do livro e ajustadas por eventos.

    Duas consultas: uma traz as transacoes com os ativos (`selectinload`),
    outra traz os desdobramentos desses ativos. O calculo roda em memoria. A
    alternativa -- uma consulta por ativo -- seria N+1 disfarcado de "codigo
    organizado".

    ## Por que o ajuste acontece AQUI

    Este e o unico caminho pelo qual uma posicao nasce: o resumo, os
    snapshots, a fronteira e os proventos todos passam por ele. Ajustar em um
    ponto so garante que nao existe um lugar do sistema onde a WEGE3 tem 100
    acoes e outro onde ela tem 200 -- o tipo de divergencia que ninguem nota
    ate um numero nao fechar.

    O livro no banco NAO e reescrito. Ele guarda o que a pessoa realmente
    fez; o ajuste e uma leitura em cima disso, refeita a cada consulta. Um
    evento corrigido ou removido depois corrige a carteira sozinho, sem
    migracao de dados -- mesma escolha do preco medio e dos proventos.
    """
    stmt = _da_carteira(portfolio_id).options(selectinload(Transaction.asset))
    transacoes = list((await db.execute(stmt)).scalars().all())
    if not transacoes:
        return []

    eventos = await split_service.dos_ativos(db, {t.asset_id for t in transacoes})
    ajustadas = split.ajustar(transacoes, eventos)
    return sorted(calcular_posicoes(ajustadas).values(), key=lambda p: p.ticker)


async def eventos_aplicados(
    db: AsyncSession, portfolio_id: uuid.UUID
) -> dict[str, list[split_service.EventoComTicker]]:
    """Eventos corporativos que de fato mexeram em cada ativo desta carteira.

    So os posteriores a primeira compra do ticker: um desdobramento de 2018 nao
    diz respeito a quem comprou em 2020. Serve a interface, para ela explicar
    por que a posicao mostra mais cotas do que o extrato.
    """
    stmt = _da_carteira(portfolio_id).options(selectinload(Transaction.asset))
    transacoes = list((await db.execute(stmt)).scalars().all())
    if not transacoes:
        return {}

    eventos = await split_service.dos_ativos(db, {t.asset_id for t in transacoes})
    return split.eventos_por_ticker(transacoes, eventos)  # type: ignore[return-value]
