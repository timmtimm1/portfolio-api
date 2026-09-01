"""Todos os models sao importados aqui.

O Alembic descobre as tabelas por `Base.metadata`, e uma tabela so entra nesse
metadata se a classe tiver sido importada. Um model que existe mas nunca e
importado simplesmente nao aparece na migration -- e, pior, o autogenerate pode
gerar um DROP TABLE achando que ela sobra no banco.
"""

from app.models.asset import Asset, AssetType, PriceHistory
from app.models.base import Base
from app.models.benchmark import BenchmarkRate, Indexador
from app.models.dividend import Dividend, TipoProvento
from app.models.portfolio import Portfolio, TipoCarteira
from app.models.quote import PriceQuote
from app.models.refresh_token import MotivoRevogacao, RefreshToken
from app.models.snapshot import PortfolioSnapshot
from app.models.split import Split
from app.models.transaction import Transaction, TransactionSide
from app.models.user import User

__all__ = [
    "Asset",
    "AssetType",
    "BenchmarkRate",
    "Base",
    "Dividend",
    "Indexador",
    "PriceHistory",
    "Portfolio",
    "PortfolioSnapshot",
    "TipoCarteira",
    "TipoProvento",
    "MotivoRevogacao",
    "PriceQuote",
    "RefreshToken",
    "Split",
    "Transaction",
    "TransactionSide",
    "User",
]
