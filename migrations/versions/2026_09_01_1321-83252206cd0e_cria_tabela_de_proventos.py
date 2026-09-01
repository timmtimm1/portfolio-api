"""cria tabela de proventos

Revision ID: 83252206cd0e
Revises: bc35731d22ea
Create Date: 2026-09-01 13:21:07.531833

## Nota sobre o autogenerate

O `--autogenerate` produziu, alem desta tabela, cinco `drop_constraint` sobre os
CHECK de enum de outras tabelas (assets.tipo, benchmark_rates.indexador,
portfolios.tipo, refresh_tokens.revoked_reason, transactions.side). Todos foram
REMOVIDOS a mao daqui.

Eles sao falso positivo: as restricoes existem no banco e estao corretas
(conferido com `pg_get_constraintdef`). O autogenerate reconstroi CHECK
declarados explicitamente, mas os que nascem de `Enum(create_constraint=True)`
sao emitidos na hora do DDL e nao aparecem no metadata da forma que ele espera
-- entao ele le no banco, nao acha no modelo, e conclui que sobram.

Aplicar o arquivo como veio teria apagado a protecao de dominio de cinco
colunas sem erro nenhum: o banco passaria a aceitar qualquer string em
`transactions.side`. Migration gerada automaticamente se le antes de rodar.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "83252206cd0e"
down_revision: str | Sequence[str] | None = "bc35731d22ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria `dividends`: proventos por ATIVO, nao por usuario.

    Um provento e um fato do mercado, como um fechamento. Quanto cada usuario
    recebeu e derivado do livro de transacoes na data-com -- ver o docstring de
    `app/models/dividend.py` para o motivo de nao existir `user_id` aqui.
    """
    op.create_table(
        "dividends",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("data_com", sa.Date(), nullable=False),
        sa.Column(
            "tipo",
            sa.Enum(
                "dividendo",
                "jcp",
                "rendimento",
                "indefinido",
                name="tipoprovento",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("valor_por_cota", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("data_pagamento", sa.Date(), nullable=True),
        sa.Column("fonte", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_dividends_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", "data_com", "tipo", name=op.f("pk_dividends")),
    )
    op.create_index("ix_dividends_data_com", "dividends", ["data_com"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_dividends_data_com", table_name="dividends")
    op.drop_table("dividends")
