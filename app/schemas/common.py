"""Schemas genericos reusados por varios recursos."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Teto rigido. O cliente pode pedir menos, nunca mais.
#
# Sem teto, `?limit=1000000` faz o banco materializar e a API serializar um
# milhao de linhas -- memoria estourada por um unico request, sem precisar de
# ataque nenhum, so de um cliente distraido. O limite e do servidor porque o
# servidor e quem paga a conta.
LIMITE_MAXIMO = 100
LIMITE_PADRAO = 20


# Sintaxe de generico do PEP 695 (Python 3.12): `class Page[T]` substitui o
# par TypeVar + Generic[T] das versoes anteriores.
class Page[T](BaseModel):
    """Envelope de paginacao.

    Devolver uma lista crua (`[...]`) e uma decisao irreversivel de API: quando
    for preciso incluir o total ou um cursor, sera mudanca quebrando o contrato.
    O envelope custa uma chave a mais hoje e evita v2 amanha.
    """

    items: list[T]
    total: int = Field(description="Total de registros que casam com o filtro")
    limit: int
    offset: int

    @property
    def tem_proxima(self) -> bool:
        return self.offset + len(self.items) < self.total
