"""meta de acumulacao por ativo e da carteira

Revision ID: b95033142c72
Revises: 0be96cdf8842
Create Date: 2026-09-02 13:24:28.293471

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b95033142c72"
down_revision: str | Sequence[str] | None = "0be96cdf8842"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Meta de acumulacao: quanto se quer ter em cada ativo, e na carteira.

    Os dois CHECK abaixo foram escritos A MAO. O `include_object` de
    `migrations/env.py` tira os check constraints da comparacao do
    autogenerate (para nao apagar os CHECK que nascem dos enums), e o efeito
    colateral e que CHECK novo tambem nao aparece sozinho. Sem eles, um
    UPDATE manual gravaria uma meta negativa e o app so descobriria na hora
    de desenhar uma barra de progresso invertida.
    """
    op.add_column(
        "asset_targets", sa.Column("meta_valor", sa.Numeric(precision=18, scale=6), nullable=True)
    )
    op.add_column(
        "portfolios", sa.Column("meta_valor", sa.Numeric(precision=18, scale=6), nullable=True)
    )
    op.create_check_constraint(
        "meta_valor_positiva", "asset_targets", "meta_valor IS NULL OR meta_valor > 0"
    )
    op.create_check_constraint(
        "meta_valor_positiva", "portfolios", "meta_valor IS NULL OR meta_valor > 0"
    )


def downgrade() -> None:
    # Os CHECK caem junto com as colunas; nao precisam de drop explicito.
    op.drop_column("portfolios", "meta_valor")
    op.drop_column("asset_targets", "meta_valor")
