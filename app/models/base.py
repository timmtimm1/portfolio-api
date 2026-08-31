"""Base declarativa comum a todos os models."""

from __future__ import annotations

import enum as enum_py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Convencao de nomes para constraints e indices.
#
# Por que isso importa: sem ela o Postgres batiza as constraints sozinho e o
# Alembic gera migrations com nomes que ele mesmo nao consegue referenciar depois
# ("could not find constraint"). Definir a convencao no dia 1 e barato; descobrir
# a falta dela na 15a migration, em producao, e caro.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Chave primaria UUID em vez de inteiro sequencial.

    Motivo de seguranca: com `id` sequencial, `/users/1` revela que existe um
    usuario 1 e convida a percorrer 2, 3, 4... Uma falha de autorizacao vira
    vazamento em massa. Com UUID nao ha o que enumerar -- e defesa em profundidade,
    nao substituto da checagem de autorizacao (essa vem na Etapa 3).

    Motivo de escalabilidade: o id e gerado na aplicacao, sem ida ao banco e sem
    contencao de sequence -- importante quando houver varios workers escrevendo.
    """

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """created_at / updated_at preenchidos pelo **banco**, nao pela aplicacao.

    `server_default=func.now()` e `onupdate` no servidor garantem o carimbo mesmo
    quando a linha e alterada por uma migration ou por SQL manual. Se a aplicacao
    fosse a unica fonte, qualquer escrita fora dela produziria dado inconsistente.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def coluna_enum[E: enum_py.Enum](tipo: type[E], *, length: int) -> Enum:
    """Tipo de coluna para enums, com duas correcoes em relacao ao padrao.

    **`values_callable`** faz o banco guardar o VALOR do membro ("compra"), nao o
    NOME ("COMPRA"), que e o padrao do SQLAlchemy. Guardar o nome cria uma
    divergencia silenciosa: a API expoe "compra", o banco tem "COMPRA", e
    qualquer SQL escrito a mao ou migration com dado literal erra. Foi
    exatamente o que aconteceu ao introduzir carteiras -- a migration inseriu
    'real' e a leitura estourou `LookupError`. Alem disso, o valor sobrevive a
    renomear o membro em Python; o nome, nao.

    **`create_constraint=True`** porque o SQLAlchemy 2.0 NAO cria o CHECK por
    padrao (mudou na 1.4). Sem ele, `native_enum=False` produz um VARCHAR sem
    restricao nenhuma -- e o banco aceita qualquer string. A validacao ficava so
    na aplicacao, que e justamente o que a Etapa 6 argumentou nao bastar: dado
    invalido que entra por um script ou por um UPDATE manual e dado invalido
    para sempre.
    """
    return Enum(
        tipo,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        create_constraint=True,
    )
