"""Gestao das carteiras do usuario."""

from __future__ import annotations

import uuid

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.portfolio import Portfolio, TipoCarteira
from app.models.user import User
from app.schemas.portfolio_schema import PortfolioCreate
from app.services.exceptions import DomainError

# Teto por usuario. Nao ha uso legitimo para centenas de carteiras, e sem limite
# um script poderia criar milhoes de linhas com um POST em laco.
MAXIMO_POR_USUARIO = 20

NOME_PADRAO_REAL = "Carteira real"


class NomeDeCarteiraEmUsoError(DomainError):
    pass


class LimiteDeCarteirasError(DomainError):
    pass


class CarteiraRealNaoApagavelError(DomainError):
    """A carteira real nao pode ser excluida.

    Ela e criada sozinha, e o padrao do sistema e a que abre selecionada --
    ou seja, a mais facil de apagar sem querer. E a exclusao e destrutiva de
    verdade: leva junto o livro inteiro e todo o historico de snapshots.

    A protecao mora AQUI, e nao so no botao. Esconder a lixeira na tela nao
    protege nada: quem chamar a API direto, ou um dia em que a interface tiver
    um defeito, passa por cima. Regra de dominio se defende no dominio.
    """


async def listar(db: AsyncSession, user_id: uuid.UUID) -> list[Portfolio]:
    """A real primeiro, depois as simuladas por nome.

    Ordem estavel e deliberada: o seletor da interface nao pode trocar de ordem
    entre dois carregamentos, e a carteira real e a que o usuario mais abre.
    """
    # Ordenacao EXPLICITA por semantica, nao por acidente alfabetico.
    #
    # A primeira versao usava `Portfolio.tipo.desc()` presumindo que "real"
    # viria antes de "simulada". Funcionava enquanto o banco guardava o NOME do
    # membro; quando passou a guardar o VALOR minusculo, 'simulada' > 'real' na
    # ordem alfabetica e o `desc()` inverteu tudo -- a carteira PADRAO virou a
    # simulada, e uma transacao lancada sem `portfolio_id` iria para a carteira
    # errada, sem erro nenhum.
    #
    # Um CASE diz o que se quer ("a real primeiro") em vez de depender de como
    # as strings se ordenam.
    stmt = (
        select(Portfolio)
        .where(Portfolio.user_id == user_id)
        .order_by(case((Portfolio.tipo == TipoCarteira.REAL, 0), else_=1), Portfolio.nome)
    )
    return list((await db.execute(stmt)).scalars().all())


async def obter(db: AsyncSession, user_id: uuid.UUID, portfolio_id: uuid.UUID) -> Portfolio | None:
    """Busca por id E por dono, na mesma consulta.

    E o ponto de autorizacao de todo o recurso: com varias carteiras, a falha
    seria aceitar um `portfolio_id` do cliente sem conferir de quem ele e -- e ai
    qualquer um leria a carteira alheia trocando um UUID na URL.
    """
    stmt = select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def obter_padrao(db: AsyncSession, usuario: User) -> Portfolio:
    """A carteira usada quando o cliente nao informa nenhuma.

    Cria a "Carteira real" na primeira vez. Sem isso, um usuario novo precisaria
    criar uma carteira antes de conseguir lancar a primeira transacao -- uma
    etapa a mais para quem so quer registrar uma compra.

    Recebe o USUARIO, e nao o `user_id`, porque a resposta depende do tipo de
    conta. Numa demo, criar uma "Carteira real" vazia seria o pior resultado
    possivel: ela viraria a padrao, e a demonstracao abriria justamente na tela
    vazia que ela existe para evitar -- com os dados semeados escondidos atras
    de um seletor que o visitante nao sabe que precisa mexer.
    """
    user_id = usuario.id
    if usuario.is_demo:
        # A carteira semeada E a padrao da demo. `order_by` para o resultado ser
        # estavel se o visitante criar outras simuladas durante a visita.
        demo = (
            await db.execute(
                select(Portfolio)
                .where(Portfolio.user_id == user_id)
                .order_by(Portfolio.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if demo is not None:
            return demo
    # Procura a carteira REAL, especificamente -- nao "a primeira da lista".
    #
    # A primeira versao criava a real so quando NAO havia carteira nenhuma. Bastava
    # o usuario criar uma simulada antes da primeira compra para a real nunca
    # existir, e ai a padrao virava a simulada: uma transacao lancada sem
    # `portfolio_id` ia para a carteira de simulacao, sem erro nenhum. Dado
    # financeiro no lugar errado, em silencio.
    existente = (
        await db.execute(
            select(Portfolio).where(
                Portfolio.user_id == user_id, Portfolio.tipo == TipoCarteira.REAL
            )
        )
    ).scalar_one_or_none()
    if existente is not None:
        return existente

    carteira = Portfolio(user_id=user_id, nome=NOME_PADRAO_REAL, tipo=TipoCarteira.REAL)
    db.add(carteira)
    try:
        await db.commit()
    except IntegrityError:
        # Corrida: dois requests do mesmo usuario chegando juntos. A constraint
        # UNIQUE resolve, e relemos em vez de estourar.
        await db.rollback()
        return (
            await db.execute(
                select(Portfolio).where(
                    Portfolio.user_id == user_id, Portfolio.tipo == TipoCarteira.REAL
                )
            )
        ).scalar_one()
    await db.refresh(carteira)
    return carteira


async def criar(db: AsyncSession, user_id: uuid.UUID, dados: PortfolioCreate) -> Portfolio:
    quantas = len(await listar(db, user_id))
    if quantas >= MAXIMO_POR_USUARIO:
        raise LimiteDeCarteirasError(str(MAXIMO_POR_USUARIO))

    carteira = Portfolio(user_id=user_id, nome=dados.nome, tipo=dados.tipo)
    db.add(carteira)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Mesma logica do cadastro de usuario: a fonte da verdade e a constraint
        # UNIQUE, que e atomica -- nao um SELECT antes do INSERT, que e corrida.
        await db.rollback()
        raise NomeDeCarteiraEmUsoError(dados.nome) from exc
    await db.refresh(carteira)
    return carteira


async def remover(db: AsyncSession, user_id: uuid.UUID, portfolio_id: uuid.UUID) -> bool:
    """Apaga a carteira e, por cascade, suas transacoes e snapshots.

    Diferente do resto do sistema, aqui a exclusao e destrutiva de verdade -- e
    por isso a interface confirma antes. Um `is_active` na carteira so adiaria a
    decisao e encheria o seletor de itens que o usuario ja descartou.

    A carteira REAL e recusada: ver `CarteiraRealNaoApagavelError`. Devolve
    `False` (e nao levanta) quando a carteira nao existe ou e de outro usuario,
    para o chamador responder 404 sem distinguir os dois casos.
    """
    carteira = await obter(db, user_id, portfolio_id)
    if carteira is None:
        return False
    if carteira.tipo is TipoCarteira.REAL:
        raise CarteiraRealNaoApagavelError(
            "A carteira real nao pode ser apagada. Ela guarda suas operacoes de verdade."
        )
    await db.delete(carteira)
    await db.commit()
    return True
