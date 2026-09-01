"""Eventos que mudam a quantidade de cotas sem nenhuma transacao acontecer."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Split(Base):
    """Desdobramento, grupamento ou bonificacao.

    ## O problema que isto resolve

    Quando a WEGE3 desdobra 2:1, quem tinha 100 acoes passa a ter 200 -- e o
    livro de transacoes continua dizendo 100, porque nenhuma operacao
    aconteceu. A partir dali, quantidade, preco medio, valor de mercado e todo
    o historico ficam errados. E o pior tipo de erro deste projeto: nada
    quebra, nenhum teste falha, e o numero na tela continua plausivel.

    Num grupamento a conta vai para o outro lado: a MGLU3 agrupou 1:10 em
    2024, entao 1000 acoes viraram 100. Um sistema que ignora o evento mostra
    uma carteira valendo dez vezes o que vale.

    ## Por que numerador e denominador, e nao um fator so

    `fator = numerador / denominador` seria 0.3333... para um 1:3, e guardar
    isso arredondado espalharia o erro por todo o historico. Guardando os dois
    inteiros, a divisao acontece em Decimal na hora do uso, com a precisao do
    contexto -- e o dado gravado continua sendo exatamente o que a empresa
    anunciou.

    ## Nao tem user_id, pelo mesmo motivo da tabela de proventos

    Desdobramento e fato do mercado: vale para todo mundo que tinha o papel.
    O efeito na SUA carteira e derivado do livro, nao gravado.
    """

    __tablename__ = "splits"
    __table_args__ = (
        # Denominador zero tornaria o fator infinito e corromperia toda posicao
        # do ativo em silencio. O banco recusa antes de chegar la.
        CheckConstraint("numerador > 0", name="numerador_positivo"),
        CheckConstraint("denominador > 0", name="denominador_positivo"),
        Index("ix_splits_data_ex", "data_ex"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )

    # Data-ex: a partir DESTE dia o papel ja negocia na quantidade nova.
    # Quem comprou NO dia ja comprou ajustado -- por isso o ajuste vale para
    # transacoes estritamente ANTERIORES a esta data.
    data_ex: Mapped[date_type] = mapped_column(Date, primary_key=True)

    # "2:1" -> numerador 2, denominador 1 (cada acao vira duas).
    # "1:10" -> numerador 1, denominador 10 (dez acoes viram uma).
    # "103:100" -> bonificacao de 3%.
    numerador: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    denominador: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)

    @property
    def fator(self) -> Decimal:
        """Por quanto a quantidade e MULTIPLICADA.

        Maior que 1 em desdobramento e bonificacao, menor que 1 em grupamento.
        O preco medio anda no sentido inverso, e o custo total nao muda -- o
        investidor nao ganhou nem perdeu dinheiro, so passou a ter o mesmo
        valor repartido em mais (ou menos) pedacos.
        """
        return self.numerador / self.denominador

    def __repr__(self) -> str:
        return f"<Split {self.asset_id} {self.data_ex} {self.numerador}:{self.denominador}>"
