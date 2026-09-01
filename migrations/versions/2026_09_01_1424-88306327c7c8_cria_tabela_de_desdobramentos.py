"""cria tabela de desdobramentos

Revision ID: 88306327c7c8
Revises: 83252206cd0e
Create Date: 2026-09-01 14:24:23.496293

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "88306327c7c8"
down_revision: str | Sequence[str] | None = "83252206cd0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Cria `splits`: eventos que mudam a quantidade sem transacao nenhuma.

    Desdobramento, grupamento e bonificacao. Como a tabela de proventos, isto
    e dado de MERCADO -- o efeito em cada carteira e derivado do livro na hora
    da leitura, nunca gravado.

    Os dois CHECK nao sao decorativos: denominador zero tornaria o fator
    infinito e corromperia toda posicao do ativo em silencio.
    """
    op.create_table(
        "splits",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("data_ex", sa.Date(), nullable=False),
        sa.Column("numerador", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("denominador", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.CheckConstraint("denominador > 0", name=op.f("ck_splits_denominador_positivo")),
        sa.CheckConstraint("numerador > 0", name=op.f("ck_splits_numerador_positivo")),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], name=op.f("fk_splits_asset_id_assets"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("asset_id", "data_ex", name=op.f("pk_splits")),
    )
    op.create_index("ix_splits_data_ex", "splits", ["data_ex"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_splits_data_ex", table_name="splits")
    op.drop_table("splits")
