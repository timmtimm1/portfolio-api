"""conta de demonstracao com validade

Revision ID: 5cbc599d4df2
Revises: 88306327c7c8
Create Date: 2026-09-01 20:03:24.642786

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5cbc599d4df2"
down_revision: str | Sequence[str] | None = "88306327c7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Marca conta de demonstracao e da validade a ela.

    `server_default="false"` NAO e decorativo. O autogenerate produziu a coluna
    como NOT NULL sem padrao, e isso falha em qualquer tabela que ja tenha
    linhas: o Postgres nao sabe o que gravar nos usuarios existentes. Aqui sao
    duas contas; em producao seriam todas.

    O default fica so no BANCO, para esta migration. O model nao o declara --
    quem cria um usuario pelo ORM passa o valor explicitamente, e depender de
    um default do banco esconderia a intencao no lugar errado.

    `expires_at` e nulo de proposito nas contas existentes: conta de verdade
    nao expira, e `NULL` diz exatamente isso.
    """
    op.add_column(
        "users",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "expires_at")
    op.drop_column("users", "is_demo")
