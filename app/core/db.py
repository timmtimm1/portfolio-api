"""Engine, sessao e dependencia de banco de dados (SQLAlchemy 2.0 async)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Engine unico por processo (o pool de conexoes vive dentro dele).

    Criar engine por request e um erro que derruba o banco: cada um abre seu
    proprio pool e o Postgres estoura o max_connections.
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        # Verifica a conexao antes de entregar do pool. Sem isso, qualquer
        # conexao morta -- e em Postgres serverless (Neon) elas morrem o tempo
        # todo por scale-to-zero -- vira um 500 aleatorio para o usuario.
        pool_pre_ping=True,
        # Recicla antes do timeout de ociosidade tipico de proxy/pgbouncer.
        pool_recycle=1800,
        pool_size=5,
        max_overflow=10,
        # echo=True imprime todo o SQL com os valores ligados -- ou seja, email,
        # hash e tudo mais indo para o log. Fica desligado por padrao e so pode
        # ser ativado fora de producao, via SQL_ECHO=true no .env.
        echo=settings.SQL_ECHO and not settings.is_production,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        # expire_on_commit=False: sem isso, ler qualquer atributo de um objeto
        # depois do commit dispara um SELECT novo -- que em codigo async estoura
        # `MissingGreenlet`. E a causa numero um de bug obscuro em FastAPI + ORM.
        expire_on_commit=False,
        autoflush=False,
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependencia de request: uma sessao por request, sempre fechada.

    Nao damos commit aqui. Quem decide o que e uma transacao completa e a camada
    de servico -- um commit automatico no fim do request esconde escrita parcial
    e torna impossivel raciocinar sobre atomicidade.

    O rollback explicito garante que uma excepcao no meio de um handler nunca
    devolva a conexao ao pool com transacao aberta e segurando lock.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Fecha o pool no shutdown. Sem isso o processo pode nao encerrar limpo e
    deixar conexoes penduradas no Postgres ate o timeout."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
