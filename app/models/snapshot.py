"""Fotografia diaria da carteira."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PortfolioSnapshot(Base, TimestampMixin):
    """Valor da carteira num dia.

    ## Por que guardar, se tudo e derivado do livro?

    A posicao e reconstruivel a qualquer momento -- mas o VALOR DE MERCADO de
    ontem nao e. Ele dependia da cotacao de ontem, e a cotacao atual so existe
    no cache, que e sobrescrito. Sem snapshot, a pergunta "como minha carteira
    evoluiu nos ultimos 6 meses?" fica sem resposta para sempre: a informacao
    nao esta em lugar nenhum para ser recuperada depois.

    E a excecao deliberada a regra "nao duplique a verdade" que vem sendo seguida
    desde a Etapa 6. A regra vale para dado DERIVAVEL; isto aqui e um fato
    historico observado, e fato historico se registra.

    ## A chave primaria composta

    (user_id, date) faz o banco garantir um snapshot por usuario por dia. O job
    pode rodar duas vezes -- por nova tentativa, por disparo manual, por dois
    workers -- e o resultado e o mesmo. Idempotencia imposta pelo schema, nao
    pela disciplina de quem chama.
    """

    __tablename__ = "portfolio_snapshots"

    # A chave e (carteira, dia), nao (usuario, dia): com varias carteiras, uma
    # foto por usuario por dia colidiria entre elas -- e a simulada sobrescreveria
    # a real, silenciosamente.
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date_type] = mapped_column(Date, primary_key=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    custo_total: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    valor_mercado: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    resultado_nao_realizado: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    resultado_realizado: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # Quantos ativos tinham cotacao no momento da foto. Se um dia o fornecedor
    # estiver fora, o snapshot registra valor pelo custo -- e este campo e o que
    # permite saber, meses depois, que aquele ponto do grafico e menos confiavel.
    ativos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ativos_sem_cotacao: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<PortfolioSnapshot {self.user_id} {self.date} {self.valor_mercado}>"
