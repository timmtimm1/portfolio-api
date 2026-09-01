"""Contrato dos fornecedores de cotacao.

O modelo de dominio fala "PETR4". Cada fornecedor tem sua propria convencao
(o Yahoo quer "PETR4.SA", a brapi quer "PETR4") -- e traduzir isso e trabalho do
adaptador, nunca do resto da aplicacao. E por isso que o banco guarda o ticker
limpo: trocar de fornecedor e trocar um arquivo desta pasta, nao uma migration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class Cotacao:
    ticker: str
    preco: Decimal
    fonte: str


@dataclass(frozen=True)
class ProventoBruto:
    """Um provento como o FORNECEDOR informa, antes de virar linha no banco.

    "Bruto" em dois sentidos: e o valor anunciado, sem desconto de retencao, e
    e o dado cru do fornecedor, sem o tipo classificado. O Yahoo devolve data e
    valor, e mais nada -- nao diz se foi dividendo ou JCP. Essa distincao vale
    15% de imposto na fonte, entao ela nao pode ser chutada aqui; quem decide e
    o servico, que grava como INDEFINIDO ate alguem corrigir.
    """

    data_com: date_type
    valor_por_cota: Decimal


class ProvedorDeCotacoes(Protocol):
    """Um fornecedor sabe buscar varios tickers de uma vez.

    A interface e em LOTE de proposito. Se fosse `cotacao(ticker)`, quem chamasse
    faria um laco -- e uma carteira de 30 ativos viraria 30 requisicoes HTTP
    externas dentro de um request de usuario. A forma da interface e o que
    impede o mau uso.
    """

    @property
    def nome(self) -> str: ...

    async def cotacoes(self, tickers: Sequence[str]) -> dict[str, Cotacao]:
        """Devolve o que conseguiu. Ticker ausente do resultado = sem cotacao.

        Nao levanta excecao por ticker inexistente: numa carteira de 30 ativos,
        um papel deslistado nao pode derrubar a consulta dos outros 29.
        """
        ...
