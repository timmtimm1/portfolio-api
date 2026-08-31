"""Todos os models sao importados aqui.

O Alembic descobre as tabelas por `Base.metadata`, e uma tabela so entra nesse
metadata se a classe tiver sido importada. Um model que existe mas nunca e
importado simplesmente nao aparece na migration -- e, pior, o autogenerate pode
gerar um DROP TABLE achando que ela sobra no banco.
"""

from app.models.asset import Asset, AssetType, PriceHistory
from app.models.base import Base
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = ["Asset", "AssetType", "Base", "PriceHistory", "RefreshToken", "User"]
