"""carteiras multiplas com backfill

Revision ID: 06ef2751118e
Revises: 7b60780ffc35
Create Date: 2026-08-31 20:07:58.559993

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "06ef2751118e"
down_revision: str | Sequence[str] | None = "7b60780ffc35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Introduz carteiras multiplas SEM perder um unico registro.

    A ordem importa e nao e negociavel:

    1. cria a tabela e as colunas como NULL -- adicionar coluna NOT NULL numa
       tabela com linhas falha na hora;
    2. cria uma "Carteira real" para cada usuario que ja tem dados;
    3. aponta as linhas existentes para ela;
    4. so entao torna as colunas obrigatorias.

    Inverter os passos 1 e 4 e o erro classico de migration com dados: funciona
    perfeitamente no banco vazio do desenvolvedor e falha em producao.
    """
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("nome", sa.String(length=60), nullable=False),
        sa.Column("tipo", sa.String(length=10), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("tipo IN ('real', 'simulada')", name=op.f("ck_portfolios_tipo")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_portfolios_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolios")),
        sa.UniqueConstraint("user_id", "nome", name="uq_portfolios_user_id_nome"),
    )
    op.create_index(op.f("ix_portfolios_user_id"), "portfolios", ["user_id"])

    op.add_column("transactions", sa.Column("portfolio_id", sa.Uuid(), nullable=True))
    op.add_column("portfolio_snapshots", sa.Column("portfolio_id", sa.Uuid(), nullable=True))

    # --- Migracao de dados -------------------------------------------------
    # `gen_random_uuid()` e nativo do Postgres desde a versao 13: nao precisamos
    # de extensao nem de um laco em Python lendo e escrevendo linha a linha.
    op.execute(
        """
        INSERT INTO portfolios (id, user_id, nome, tipo, created_at, updated_at)
        SELECT gen_random_uuid(), u.id, 'Carteira real', 'real', now(), now()
        FROM users u
        WHERE EXISTS (SELECT 1 FROM transactions t WHERE t.user_id = u.id)
           OR EXISTS (SELECT 1 FROM portfolio_snapshots s WHERE s.user_id = u.id)
        """
    )
    op.execute(
        """
        UPDATE transactions t SET portfolio_id = p.id
        FROM portfolios p WHERE p.user_id = t.user_id AND p.nome = 'Carteira real'
        """
    )
    op.execute(
        """
        UPDATE portfolio_snapshots s SET portfolio_id = p.id
        FROM portfolios p WHERE p.user_id = s.user_id AND p.nome = 'Carteira real'
        """
    )

    # Linha orfa nao pode existir: se algo escapou do backfill, e melhor a
    # migration falhar aqui do que o dado sumir da carteira de alguem.
    op.execute("DELETE FROM portfolio_snapshots WHERE portfolio_id IS NULL")

    op.alter_column("transactions", "portfolio_id", nullable=False)
    op.alter_column("portfolio_snapshots", "portfolio_id", nullable=False)

    op.create_foreign_key(
        op.f("fk_transactions_portfolio_id_portfolios"),
        "transactions",
        "portfolios",
        ["portfolio_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_portfolio_snapshots_portfolio_id_portfolios"),
        "portfolio_snapshots",
        "portfolios",
        ["portfolio_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_transactions_portfolio_id"), "transactions", ["portfolio_id"])

    # A chave do snapshot passa de (usuario, dia) para (carteira, dia): com
    # varias carteiras, a antiga faria a simulada sobrescrever a real.
    op.drop_constraint("pk_portfolio_snapshots", "portfolio_snapshots", type_="primary")
    op.create_primary_key("pk_portfolio_snapshots", "portfolio_snapshots", ["portfolio_id", "date"])
    op.create_index(op.f("ix_portfolio_snapshots_user_id"), "portfolio_snapshots", ["user_id"])

    op.drop_index("ix_transactions_user_asset_data", table_name="transactions")
    op.create_index(
        "ix_transactions_carteira_ativo_data",
        "transactions",
        ["portfolio_id", "asset_id", "traded_at"],
    )


def downgrade() -> None:
    """Volta ao modelo de uma carteira por usuario.

    Perde a separacao entre real e simulada -- as transacoes de todas as
    carteiras passam a conviver. E irreversivel no sentido semantico, e por isso
    esta escrito aqui em vez de descoberto depois.
    """
    op.drop_index("ix_transactions_carteira_ativo_data", table_name="transactions")
    op.create_index(
        "ix_transactions_user_asset_data", "transactions", ["user_id", "asset_id", "traded_at"]
    )
    op.drop_index(op.f("ix_portfolio_snapshots_user_id"), table_name="portfolio_snapshots")
    op.drop_constraint("pk_portfolio_snapshots", "portfolio_snapshots", type_="primary")
    # Duas carteiras podem ter foto no mesmo dia; ao voltar, so uma sobrevive.
    op.execute(
        """
        DELETE FROM portfolio_snapshots a USING portfolio_snapshots b
        WHERE a.user_id = b.user_id AND a.date = b.date AND a.ctid > b.ctid
        """
    )
    op.create_primary_key("pk_portfolio_snapshots", "portfolio_snapshots", ["user_id", "date"])

    op.drop_index(op.f("ix_transactions_portfolio_id"), table_name="transactions")
    op.drop_constraint(
        op.f("fk_portfolio_snapshots_portfolio_id_portfolios"),
        "portfolio_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_transactions_portfolio_id_portfolios"), "transactions", type_="foreignkey"
    )
    op.drop_column("portfolio_snapshots", "portfolio_id")
    op.drop_column("transactions", "portfolio_id")
    op.drop_index(op.f("ix_portfolios_user_id"), table_name="portfolios")
    op.drop_table("portfolios")
