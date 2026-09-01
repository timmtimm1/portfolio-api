"""ibov entra no indexador de comparacao

Revision ID: bc35731d22ea
Revises: 091c1a39886d
Create Date: 2026-09-01 12:27:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bc35731d22ea"
down_revision: str | Sequence[str] | None = "091c1a39886d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ver a migration `091c1a39886d` para a explicacao completa do prefixo dobrado
# e de por que `op.f()` e chamado dentro das funcoes, nunca guardado pronto
# numa constante do modulo.
CONSTRAINT_NOME = "ck_benchmark_rates_ck_benchmark_rates_indexador"


def upgrade() -> None:
    """Permite 'ibov' na coluna que ate aqui aceitava 'cdi', 'selic' e 'ipca'."""
    op.drop_constraint(op.f(CONSTRAINT_NOME), "benchmark_rates", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT_NOME),
        "benchmark_rates",
        "indexador IN ('cdi', 'selic', 'ipca', 'ibov')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT_NOME), "benchmark_rates", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT_NOME), "benchmark_rates", "indexador IN ('cdi', 'selic', 'ipca')"
    )
