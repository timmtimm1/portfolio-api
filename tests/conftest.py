"""Configuracao da suite de testes.

Tres decisoes que definem a qualidade desta suite:

1. **Postgres de verdade, num container efemero** -- nao SQLite.
   Testar em SQLite e rodar em Postgres e testar outro banco. SQLite nao tem
   UUID nativo, ignora o tamanho de VARCHAR, trata timezone de outro jeito e nao
   tem a mesma semantica de constraint. Metade dos bugs que a suite existe para
   pegar sao exatamente esses. O container sobe uma vez por sessao.

2. **O schema vem das migrations, nao de `create_all()`.**
   `Base.metadata.create_all()` monta o schema a partir dos models -- ou seja,
   testa contra um banco que talvez nunca exista em producao. Rodando o Alembic,
   cada `pytest` verifica de graca que as migrations aplicam do zero. Migration
   quebrada passa a falhar no CI, nao no deploy.

3. **Cada teste roda numa transacao que sofre rollback no fim.**
   Muito mais rapido que recriar o banco, e garante isolamento total: nenhum
   teste ve o dado do outro, e a ordem de execucao deixa de importar. Bug de
   suite que "so falha quando roda tudo junto" nasce justamente da falta disso.
"""

from __future__ import annotations

import os

# Estas variaveis PRECISAM existir antes de qualquer import de `app`: o modulo de
# rate limit le a configuracao no momento do import. ENVIRONMENT=test desliga o
# limitador -- uma suite que dispara 40 logins esbarraria nele e falharia por
# motivo errado, escondendo a falha de verdade.
os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "chave-de-teste-deterministica-com-mais-de-32-caracteres"
os.environ.setdefault("POSTGRES_PASSWORD", "teste")

from collections.abc import AsyncIterator, Iterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from testcontainers.community.postgres import PostgresContainer  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.main import create_app  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    """Sobe um Postgres descartavel e aplica as migrations nele."""
    with PostgresContainer("postgres:17-alpine", driver=None) as container:
        os.environ["POSTGRES_HOST"] = container.get_container_host_ip()
        os.environ["POSTGRES_PORT"] = str(container.get_exposed_port(5432))
        os.environ["POSTGRES_DB"] = container.dbname
        os.environ["POSTGRES_USER"] = container.username
        os.environ["POSTGRES_PASSWORD"] = container.password

        # `get_settings` e cacheado; sem limpar, ele devolveria a configuracao
        # lida antes do container existir -- e a suite atacaria o banco de
        # desenvolvimento. Este `cache_clear` e o que impede isso.
        get_settings.cache_clear()

        cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(PROJECT_ROOT, "migrations"))
        command.upgrade(cfg, "head")

        yield container


@pytest_asyncio.fixture(scope="session")
async def engine(postgres: PostgresContainer) -> AsyncIterator[object]:
    eng = create_async_engine(get_settings().database_url, poolclass=None)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine: object) -> AsyncIterator[AsyncSession]:
    """Sessao amarrada a uma transacao externa que sempre sofre rollback.

    `join_transaction_mode="create_savepoint"` faz o `commit()` do codigo de
    producao virar a liberacao de um SAVEPOINT em vez de um commit real. Ou seja:
    o codigo testado roda exatamente como em producao, commits inclusive, mas
    nada sobrevive ao fim do teste.
    """
    conn = await engine.connect()  # type: ignore[attr-defined]
    trans = await conn.begin()
    session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Cliente HTTP que fala com a aplicacao em memoria, sem porta de rede.

    `ASGITransport` chama a aplicacao direto. Nao ha servidor, nao ha socket, nao
    ha porta ocupada -- os testes rodam em paralelo e no CI sem nenhum arranjo.

    A sobrescrita de `get_db` e o que amarra as rotas a transacao do teste. Sem
    ela, o handler abriria a propria sessao, commitaria de verdade, e o dado
    vazaria de um teste para o outro.
    """
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1") as ac:
        yield ac


@pytest.fixture
def senha_valida() -> str:
    return "carteira-b3-2026-forte"
