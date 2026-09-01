"""Ambiente do Alembic.

Duas decisoes deliberadas:

1. A URL do banco vem de `Settings` (variaveis de ambiente), **nunca** do
   alembic.ini. Credencial em arquivo versionado e uma das formas mais comuns de
   vazar senha de producao num repositorio publico -- por isso a chave
   `sqlalchemy.url` foi esvaziada no alembic.ini.

2. Migration roda com driver **sincrono** (psycopg). O template async do Alembic
   funciona, mas nao ha nada a ganhar com concorrencia num script que executa DDL
   sequencial -- e o codigo sincrono e muito mais simples de depurar quando uma
   migration falha no meio.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.models import Base  # importa todos os models -> popula Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    get_settings().database_url_sync.render_as_string(hide_password=False),
)

target_metadata = Base.metadata


def include_object(
    objeto: object, nome: str | None, tipo: str, reflected: bool, comparar_com: object
) -> bool:
    """Tira os CHECK da comparacao do autogenerate.

    ## Por que, com nome e sobrenome

    O autogenerate NAO enxerga os CHECK que nascem de `Enum(create_constraint=
    True)`: eles sao emitidos na hora do DDL e nao entram no metadata da forma
    que a comparacao espera. Resultado: ele le seis restricoes no banco, nao
    acha nenhuma no modelo, e conclui que todas sobram.

    Isso nao e cosmetico. Duas vezes num mesmo dia o `--autogenerate` produziu
    `drop_constraint` para as seis -- e aplicar o arquivo como veio teria
    deixado `transactions.side` aceitando qualquer string, sem erro nenhum. Um
    detector de drift que grita sem motivo e um detector que se aprende a
    ignorar; um que gera DDL destrutivo e pior que nao ter detector.

    ## O preco disto

    Mudanca em CHECK deixa de ser detectada e passa a ser escrita a mao (foi o
    que ja fizemos em `091c1a39886d` e `bc35731d22ea`). Em troca, `alembic
    check` volta a ser confiavel para tabela, coluna, tipo, indice e chave
    estrangeira -- que e onde o drift silencioso realmente acontece.
    """
    return not (tipo == "check_constraint")


def run_migrations_offline() -> None:
    """Gera o SQL sem conectar (`alembic upgrade head --sql`).

    Util para revisar o DDL antes de aplicar num banco de producao -- aplicar
    migration as cegas em producao e como se joga fora um banco.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type/compare_server_default: sem isso o autogenerate ignora
            # mudanca de tipo de coluna (String(50) -> String(320) passaria batido)
            # e voce descobre no runtime, com dado truncado.
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
