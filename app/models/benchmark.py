"""Taxas diarias de referencia (CDI, Selic) publicadas pelo Banco Central."""

from __future__ import annotations

import enum
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, Enum, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Indexador(enum.StrEnum):
    """Series do SGS (Sistema Gerenciador de Series Temporais) do BCB.

    CDI (serie 12) e Selic (serie 11) sao praticamente identicas no dia a dia --
    o CDI acompanha a Selic de perto. Guardamos as duas porque a comparacao
    usual de fundos e "% do CDI", enquanto o Tesouro Selic segue a Selic.
    """

    CDI = "cdi"
    SELIC = "selic"


class BenchmarkRate(Base):
    """Taxa de UM dia util.

    Chave natural (indexador, date): o BCB publica um valor por dia util, e a PK
    composta impede duplicata na sincronizacao repetida -- mesma logica de
    `price_history`.

    Nao ha fim de semana nem feriado na serie. Isso e uma propriedade util, nao
    um buraco: o CDI so rende em dia util, entao a ausencia da data ja significa
    "nao rendeu".
    """

    __tablename__ = "benchmark_rates"

    indexador: Mapped[Indexador] = mapped_column(
        Enum(Indexador, native_enum=False, length=10, validate_strings=True), primary_key=True
    )
    date: Mapped[date_type] = mapped_column(Date, primary_key=True)

    # Taxa do dia em FRACAO decimal (0.00051660), nao em percentual (0.051660).
    #
    # O BCB publica em percentual; convertemos na borda, no adaptador. Guardar a
    # unidade do fornecedor obrigaria todo calculo adiante a lembrar de dividir
    # por 100 -- e um dia alguem esquece, e o resultado erra por 100x sem
    # estourar nada.
    #
    # 10 casas decimais: a taxa diaria tem 6 casas em percentual, entao 8 em
    # fracao; a folga evita perda de precisao no acumulado de centenas de dias.
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 10), nullable=False)

    def __repr__(self) -> str:
        return f"<BenchmarkRate {self.indexador} {self.date} {self.rate}>"
