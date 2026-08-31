"""enums gravam valor e ganham CHECK

Revision ID: ed28216483b2
Revises: 06ef2751118e
Create Date: 2026-08-31 20:15:36.689806

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed28216483b2"
down_revision: str | Sequence[str] | None = "06ef2751118e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (tabela, coluna, membros do enum)
COLUNAS = [
    ("assets", "tipo", ["ACAO", "FII", "ETF", "UNIT", "BDR", "OUTRO"]),
    ("transactions", "side", ["COMPRA", "VENDA"]),
    ("refresh_tokens", "revoked_reason", ["ROTACAO", "LOGOUT", "SEGURANCA"]),
    ("benchmark_rates", "indexador", ["CDI", "SELIC"]),
]


def upgrade() -> None:
    """Passa o conteudo das colunas de enum do NOME do membro para o VALOR.

    O SQLAlchemy grava o nome ("COMPRA") por padrao, enquanto a API expoe o
    valor ("compra"). A divergencia e silenciosa ate alguem escrever SQL a mao ou
    uma migration com dado literal -- foi assim que a introducao de carteiras
    quebrou com `LookupError: 'real' is not among the defined enum values`.

    Aproveitamos para criar os CHECK que faltavam: `native_enum=False` NAO cria a
    restricao por padrao no SQLAlchemy 2.0, entao essas colunas eram VARCHAR
    livre -- o banco aceitava qualquer string.
    """
    for tabela, coluna, membros in COLUNAS:
        for membro in membros:
            op.execute(
                # S608: os VALORES vao como parametros vinculados (:novo, :velho);
                # so os identificadores sao interpolados, e vem da constante
                # COLUNAS deste proprio arquivo -- nunca de entrada externa.
                # SQL nao aceita identificador parametrizado, entao nao ha
                # alternativa aqui.
                sa.text(
                    f"UPDATE {tabela} SET {coluna} = :novo WHERE {coluna} = :velho"  # noqa: S608
                ).bindparams(novo=membro.lower(), velho=membro)
            )
        valores = ", ".join(f"'{m.lower()}'" for m in membros)
        nulo = "" if tabela != "refresh_tokens" else f"{coluna} IS NULL OR "
        op.create_check_constraint(
            f"ck_{tabela}_{coluna}", tabela, f"{nulo}{coluna} IN ({valores})"
        )


def downgrade() -> None:
    for tabela, coluna, membros in COLUNAS:
        op.drop_constraint(f"ck_{tabela}_{coluna}", tabela, type_="check")
        for membro in membros:
            op.execute(
                # S608: os VALORES vao como parametros vinculados (:novo, :velho);
                # so os identificadores sao interpolados, e vem da constante
                # COLUNAS deste proprio arquivo -- nunca de entrada externa.
                # SQL nao aceita identificador parametrizado, entao nao ha
                # alternativa aqui.
                sa.text(
                    f"UPDATE {tabela} SET {coluna} = :novo WHERE {coluna} = :velho"  # noqa: S608
                ).bindparams(novo=membro, velho=membro.lower())
            )
