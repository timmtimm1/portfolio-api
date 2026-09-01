"""Taxas diarias de referencia para comparar a carteira: CDI, Selic e IPCA, do
Banco Central, e Ibovespa, do Yahoo Finance."""

from __future__ import annotations

import enum
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, coluna_enum


class Indexador(enum.StrEnum):
    """Series do SGS (Sistema Gerenciador de Series Temporais) do BCB.

    CDI (serie 12) e Selic (serie 11) sao praticamente identicas no dia a dia --
    o CDI acompanha a Selic de perto. Guardamos as duas porque a comparacao
    usual de fundos e "% do CDI", enquanto o Tesouro Selic segue a Selic.

    IPCA (serie 433) e outra familia: o BCB publica UM valor por MES, nao por
    dia util. `BcbClient.taxas()` espalha essa taxa mensal em taxas diarias
    equivalentes antes de gravar aqui -- por isso esta tabela guarda "um dia
    util" para CDI/Selic e "um dia de calendario" para IPCA, e ambos convivem
    na mesma coluna sem o resto do sistema precisar saber a diferenca.

    IBOV nao vem do BCB: o SGS tinha uma serie (numero 7), mas foi
    DESCONTINUADA em 2019 -- pedir dados de hoje devolve "Value(s) not found".
    Vem do Yahoo Finance (ticker "^BVSP"), o mesmo fornecedor de reserva que
    `YahooClient` ja usa para cotacao de acoes. Nao e uma taxa no sentido de
    CDI/Selic -- e a variacao percentual diaria do INDICE (fechamento de hoje
    sobre o de ontem). Matematicamente isso e exatamente o que uma carteira
    investida 1:1 no Ibovespa (um ETF como BOVA11, sem taxas) teria ganho ou
    perdido naquele dia -- entao trata-se como uma taxa diaria comum, e reusa
    a mesma `curva_equivalente()` de CDI/Selic/IPCA sem nenhuma mudanca.
    """

    CDI = "cdi"
    SELIC = "selic"
    IPCA = "ipca"
    IBOV = "ibov"


class BenchmarkRate(Base):
    """Taxa de um dia (util para CDI/Selic, de calendario para IPCA -- ver o
    comentario em `Indexador`).

    Chave natural (indexador, date): o BCB publica um valor por dia util (ou,
    no caso do IPCA, o adaptador deriva um por dia de calendario), e a PK
    composta impede duplicata na sincronizacao repetida -- mesma logica de
    `price_history`.

    Para CDI/Selic, nao ha fim de semana nem feriado na serie. Isso e uma
    propriedade util, nao um buraco: o CDI so rende em dia util, entao a
    ausencia da data ja significa "nao rendeu".
    """

    __tablename__ = "benchmark_rates"

    indexador: Mapped[Indexador] = mapped_column(
        coluna_enum(Indexador, length=10), primary_key=True
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
