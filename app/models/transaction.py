"""Livro de transacoes -- a fonte da verdade da carteira."""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.asset import Asset
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TransactionSide(enum.StrEnum):
    COMPRA = "compra"
    VENDA = "venda"


class Transaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Uma compra ou venda.

    Guardamos o LIVRO, nao a posicao. Quantidade e preco medio sao derivados
    (ver app/services/position.py). Se a posicao fosse uma coluna, existiriam
    duas fontes da verdade -- e quando elas divergissem, ninguem saberia qual
    esta certa. Com o livro, o extrato sempre explica o saldo.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        # Restricoes no BANCO, alem da validacao no Pydantic.
        #
        # A validacao da API protege contra o cliente; o CHECK protege contra
        # tudo o mais -- um script de importacao, uma migration de correcao, um
        # UPDATE manual as 3h da manha. Dado invalido que entra por qualquer
        # caminho e dado invalido para sempre.
        CheckConstraint("quantity > 0", name="quantidade_positiva"),
        CheckConstraint("price > 0", name="preco_positivo"),
        CheckConstraint("fees >= 0", name="taxas_nao_negativas"),
        # Indice composto na ordem em que as consultas filtram: sempre por
        # usuario, depois por ativo, depois por data. A ordem das colunas num
        # indice composto importa -- (user_id, asset_id, traded_at) serve tanto
        # "tudo do usuario" quanto "este ativo do usuario"; a ordem inversa nao
        # serviria a primeira consulta.
        Index("ix_transactions_user_asset_data", "user_id", "asset_id", "traded_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # RESTRICT, nao CASCADE: apagar um ativo do catalogo NAO pode apagar o
    # historico de transacoes de ninguem. O banco recusa a exclusao enquanto
    # houver transacao referenciando -- que e exatamente o comportamento certo
    # para um registro financeiro.
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )

    side: Mapped[TransactionSide] = mapped_column(
        Enum(TransactionSide, native_enum=False, length=10, validate_strings=True), nullable=False
    )

    # Numeric(18,8): fracionario existe na B3 (mercado fracionario e cotas de
    # FII), entao quantidade nao e inteiro.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # Corretagem e emolumentos. Entram no custo na compra, saem do resultado na
    # venda -- e o tratamento que a Receita exige.
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0), nullable=False)

    traded_at: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String(200), default=None)

    # lazy="raise" e a defesa contra N+1 mais eficaz que existe.
    #
    # Com o padrao ("select"), acessar `transacao.asset` fora de um carregamento
    # explicito dispara um SELECT silencioso -- e uma listagem de 200 transacoes
    # vira 201 consultas sem nenhum aviso. Com "raise", esse acesso levanta
    # excecao: o N+1 falha em desenvolvimento e no teste, em vez de so aparecer
    # como lentidao em producao.
    asset: Mapped[Asset] = relationship(lazy="raise")

    @property
    def ticker(self) -> str:
        """Satisfaz o Protocol `TransacaoLike` do calculo de posicao."""
        return self.asset.ticker

    def __repr__(self) -> str:
        return f"<Transaction {self.side} {self.quantity} @ {self.price} em {self.traded_at}>"
