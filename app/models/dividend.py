"""Proventos pagos por ativo -- dividendos, JCP e rendimentos de FII."""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, coluna_enum


class TipoProvento(enum.StrEnum):
    """O tipo muda quanto o dinheiro chega na conta, nao so o nome.

    DIVIDENDO e isento na fonte para pessoa fisica. JCP sofre 15% de retencao
    -- entao o valor anunciado NAO e o valor recebido. RENDIMENTO e o nome que
    FII usa para a distribuicao mensal (isenta, sob condicoes).

    INDEFINIDO existe porque o Yahoo Finance nao classifica: ele devolve
    "TAEE11 pagou 0,60 em 17/08" e mais nada. Chutar DIVIDENDO seria uma
    mentira silenciosa que superestima o recebido em 15% quando era JCP. Melhor
    admitir que nao sabemos e deixar o usuario corrigir.
    """

    DIVIDENDO = "dividendo"
    JCP = "jcp"
    RENDIMENTO = "rendimento"
    INDEFINIDO = "indefinido"


# Retencao de imposto na fonte, por tipo. Fracao do valor bruto que NAO chega.
#
# INDEFINIDO fica em zero de proposito: sem saber o tipo, aplicar 15% inventaria
# um desconto que pode nao existir. Errar para o valor bruto e visivel e
# corrigivel; errar para menos parece certo e ninguem percebe.
RETENCAO_NA_FONTE = {
    TipoProvento.DIVIDENDO: Decimal(0),
    TipoProvento.JCP: Decimal("0.15"),
    TipoProvento.RENDIMENTO: Decimal(0),
    TipoProvento.INDEFINIDO: Decimal(0),
}


class Dividend(Base):
    """Um provento anunciado por um ativo.

    ## Por que isto NAO tem user_id

    Um provento e um fato do MERCADO, igual a um fechamento: a TAEE11 pagou
    R$ 0,60 por unit com data-com em 17/08/2026, e isso vale para todo mundo.
    Quanto VOCE recebeu e consequencia de duas coisas -- este fato e quantas
    cotas voce tinha naquele dia --, e a segunda ja esta no livro de transacoes.

    Guardar "o Bernardo recebeu R$ 27,00" numa tabela seria duplicar o que o
    livro ja sabe, e criar a possibilidade de os dois discordarem: apagar uma
    compra antiga deixaria o provento gravado para sempre, referente a cotas que
    a carteira nunca teve. Derivar na hora nao tem esse problema -- e a mesma
    razao pela qual os snapshots sao reconstruidos a partir do livro em vez de
    corrigidos no lugar.

    ## A chave

    (asset_id, data_com, tipo). A data-com entra porque e ela que decide quem
    recebe; o tipo entra porque uma empresa pode anunciar dividendo E JCP com a
    mesma data-com, e sao dois eventos distintos com tributacao distinta.
    """

    __tablename__ = "dividends"
    __table_args__ = (
        # A sincronizacao pergunta "o que ja tenho deste ativo?", mas o calculo
        # de proventos de uma carteira pergunta "o que foi pago entre estas duas
        # datas, para estes N ativos?" -- caminho inverso ao da PK.
        Index("ix_dividends_data_com", "data_com"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )

    # Data-com (ex-date): quem tinha o ativo NO FECHAMENTO deste dia recebe.
    # Comprar no dia seguinte nao da direito ao provento -- e o motivo de esta
    # coluna existir separada de `data_pagamento`, que so diz quando o dinheiro
    # cai e nao decide nada.
    data_com: Mapped[date_type] = mapped_column(Date, primary_key=True)

    tipo: Mapped[TipoProvento] = mapped_column(
        coluna_enum(TipoProvento, length=20), primary_key=True
    )

    # 8 casas decimais, contra as 6 de `price_history`: provento por cota pode
    # ser fracao de centavo (0,00042 por cota em FII grande nao e incomum), e
    # arredondar na origem espalharia o erro por toda a serie.
    valor_por_cota: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)

    # O Yahoo nao informa a data de pagamento, so a data-com. Fica nulo ate
    # alguem preencher a mao -- e nulo aqui e honesto, nao um buraco: nenhum
    # calculo depende dela hoje.
    data_pagamento: Mapped[date_type | None] = mapped_column(Date, default=None)

    # De onde veio o dado ("yahoo", "manual"). Existe para o lancamento manual
    # do usuario nao ser sobrescrito na proxima sincronizacao automatica.
    fonte: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")

    def __repr__(self) -> str:
        return f"<Dividend {self.asset_id} {self.data_com} {self.tipo} {self.valor_por_cota}>"
