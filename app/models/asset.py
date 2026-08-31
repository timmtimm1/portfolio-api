"""Catalogo de ativos da B3 e historico de fechamentos."""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import BigInteger, Date, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AssetType(enum.StrEnum):
    """Classificacao do papel.

    `StrEnum` (Python 3.11+) em vez do antigo `str, Enum`: o membro E uma string,
    entao serializa direto em JSON e aparece legivel no OpenAPI, sem conversao
    manual em cada schema.
    """

    ACAO = "acao"
    FII = "fii"
    ETF = "etf"
    UNIT = "unit"
    BDR = "bdr"
    OUTRO = "outro"


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"

    # Guardamos "PETR4", nao "PETR4.SA".
    #
    # O sufixo .SA e uma convencao do Yahoo Finance para identificar a bolsa --
    # e detalhe de UM fornecedor, nao parte da identidade do ativo. A brapi usa
    # "PETR4"; outro fornecedor usaria outra coisa. Deixar o sufixo vazar para o
    # banco amarraria o modelo de dominio ao yfinance, e trocar de fornecedor
    # (Etapa 7) viraria uma migration em vez de trocar um adaptador.
    ticker: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)

    nome: Mapped[str | None] = mapped_column(String(120), default=None)
    setor: Mapped[str | None] = mapped_column(String(80), default=None)

    # native_enum=False cria VARCHAR + CHECK em vez de um tipo ENUM do Postgres.
    #
    # Motivo pratico: adicionar um valor a um ENUM nativo exige ALTER TYPE, que
    # tem restricoes de transacao e torna a migration mais fragil; remover um
    # valor e pior ainda. Com VARCHAR + CHECK, incluir "criptomoeda" amanha e uma
    # migration trivial. O banco continua garantindo o dominio dos valores.
    tipo: Mapped[AssetType] = mapped_column(
        Enum(AssetType, native_enum=False, length=20, validate_strings=True),
        default=AssetType.OUTRO,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Asset {self.ticker}>"


class PriceHistory(Base):
    """Fechamento diario por ativo.

    Nao usa UUIDPrimaryKeyMixin: a chave e (asset_id, data). Uma chave natural
    composta aqui faz dois trabalhos de uma vez -- impede fisicamente que o mesmo
    dia entre duas vezes para o mesmo ativo (importacao repetida nao duplica) e
    ja e exatamente o indice que toda consulta usa ("os N ultimos fechamentos
    deste ativo"). Um id sinteticio exigiria a chave composta como UNIQUE alem
    da PK, ou seja, um indice a mais para manter sem ganho nenhum.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        # A consulta de metricas (Etapa 8) varre uma janela de datas para varios
        # ativos. Este indice serve o caminho inverso do da PK.
        Index("ix_price_history_date", "date"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date_type] = mapped_column(Date, primary_key=True)

    # Numeric, NUNCA float.
    #
    # Em ponto flutuante binario, 0.1 + 0.2 == 0.30000000000000004: valores
    # decimais simples nao tem representacao exata. Num preco medio calculado
    # sobre dezenas de transacoes, o erro acumula e vira centavo faltando --
    # numa aplicacao financeira isso e defeito, nao arredondamento.
    # Numeric mapeia para Decimal em Python: aritmetica decimal exata.
    # 18 digitos com 6 casas cobre desde centavo de acao ate valor de carteira.
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # BigInteger: o volume financeiro diario de PETR4 passa de 2 bilhoes, e o
    # INTEGER do Postgres estoura em 2.147.483.647.
    volume: Mapped[int | None] = mapped_column(BigInteger, default=None)

    def __repr__(self) -> str:
        return f"<PriceHistory asset={self.asset_id} {self.date} {self.close}>"
