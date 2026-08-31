"""Comparacao da carteira contra CDI / Selic.

## A pergunta que este modulo responde

Nao e "quanto rendeu o CDI no periodo?" -- e **"se eu tivesse posto o MESMO
dinheiro, nos MESMOS dias, no CDI, quanto teria hoje?"**.

A diferenca importa quando ha aportes. Aplicar o CDI so sobre o valor inicial
subestima o benchmark se voce aportou depois, e a carteira parece melhor do que
foi. Comparar percentuais simples e ainda pior: uma carteira que dobrou de
tamanho no meio do periodo tem um percentual que nao significa nada.

A curva equivalente e construida dia a dia:

    equivalente[0] = custo[0]
    equivalente[t] = equivalente[t-1] * (1 + taxa_do_dia) + aporte[t]

onde `aporte[t]` e a variacao do custo total -- exatamente o dinheiro novo que
entrou (ou saiu) naquele dia. E o mesmo tratamento que um fundo daria.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.bcb import BcbClient
from app.models.benchmark import BenchmarkRate, Indexador
from app.models.snapshot import PortfolioSnapshot

logger = logging.getLogger(__name__)
ZERO = Decimal(0)
CEM = Decimal(100)


@dataclass(frozen=True)
class PontoBenchmark:
    date: date_type
    valor: Decimal


@dataclass(frozen=True)
class PontoRentabilidade:
    """Rentabilidade ACUMULADA no periodo, em fracao (0.0415 = +4,15%)."""

    date: date_type
    carteira: Decimal
    benchmark: Decimal | None


async def taxas_do_periodo(
    db: AsyncSession,
    cliente: BcbClient,
    indexador: Indexador,
    desde: date_type,
    ate: date_type,
) -> dict[date_type, Decimal]:
    """Le do banco e busca no BCB so o que faltar.

    Mesma logica do cache de cotacoes, com uma diferenca importante: taxa passada
    NAO muda. Uma vez gravado, o CDI de um dia esta gravado para sempre -- entao
    nao ha TTL aqui, so preenchimento das lacunas.

    A verificacao de lacuna e por COBERTURA das pontas, nao por contagem: o BCB
    nao publica em fim de semana e feriado, entao "faltam dias" e o estado normal
    de qualquer intervalo.
    """
    existentes = {
        linha.date: linha.rate
        for linha in (
            await db.execute(
                select(BenchmarkRate).where(
                    BenchmarkRate.indexador == indexador,
                    BenchmarkRate.date.between(desde, ate),
                )
            )
        )
        .scalars()
        .all()
    }

    precisa_buscar = not existentes or min(existentes) > desde or max(existentes) < ate
    if not precisa_buscar:
        return existentes

    novas = await cliente.taxas(indexador, desde, ate)
    if not novas:
        # O BCB fora do ar nao pode derrubar o grafico da carteira: devolvemos o
        # que ja tinhamos, mesmo incompleto.
        return existentes

    stmt = insert(BenchmarkRate).values(
        [{"indexador": indexador, "date": dia, "rate": taxa} for dia, taxa in sorted(novas.items())]
    )
    # DO NOTHING: taxa de um dia passado nao muda, entao reescrever seria
    # trabalho sem efeito.
    await db.execute(
        stmt.on_conflict_do_nothing(index_elements=[BenchmarkRate.indexador, BenchmarkRate.date])
    )
    await db.commit()

    return {**existentes, **novas}


def curva_equivalente(
    snapshots: list[PortfolioSnapshot], taxas: dict[date_type, Decimal]
) -> list[PontoBenchmark]:
    """Quanto valeria o mesmo dinheiro, nos mesmos dias, rendendo o indexador.

    Os snapshots precisam vir em ordem CRONOLOGICA (mais antigo primeiro): a
    curva e recursiva, cada ponto depende do anterior.

    Dia sem taxa publicada (fim de semana, feriado) simplesmente nao rende --
    que e o comportamento real do CDI, nao uma aproximacao.
    """
    if not snapshots:
        return []

    curva: list[PontoBenchmark] = []
    equivalente = snapshots[0].custo_total
    custo_anterior = snapshots[0].custo_total
    curva.append(PontoBenchmark(snapshots[0].date, equivalente))

    for ponto in snapshots[1:]:
        taxa = taxas.get(ponto.date, ZERO)
        equivalente = equivalente * (1 + taxa)

        # Aporte (ou retirada) do dia: o dinheiro novo entra DEPOIS de render,
        # porque ele nao estava aplicado no dia anterior.
        aporte = ponto.custo_total - custo_anterior
        equivalente += aporte
        custo_anterior = ponto.custo_total

        curva.append(PontoBenchmark(ponto.date, equivalente))

    return curva


NOMES = {Indexador.CDI: "CDI", Indexador.SELIC: "Selic"}


async def evolucao_comparada(
    db: AsyncSession,
    cliente: BcbClient,
    user_id: object,
    indexador: Indexador,
    *,
    desde: date_type | None = None,
    ate: date_type | None = None,
    limite: int,
) -> tuple[list[PortfolioSnapshot], list[PontoBenchmark], dict[date_type, Decimal], str | None]:
    """Snapshots em ordem cronologica + a curva equivalente do indexador."""
    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id)
    if desde is not None:
        stmt = stmt.where(PortfolioSnapshot.date >= desde)
    if ate is not None:
        stmt = stmt.where(PortfolioSnapshot.date <= ate)

    # Ordem CRESCENTE aqui, ao contrario da listagem: a curva e recursiva.
    # Os ultimos `limite` pontos, mas do mais antigo para o mais novo.
    recentes = list(
        (await db.execute(stmt.order_by(PortfolioSnapshot.date.desc()).limit(limite)))
        .scalars()
        .all()
    )
    snapshots = sorted(recentes, key=lambda s: s.date)

    if len(snapshots) < 2:
        return snapshots, [], {}, "A comparacao precisa de pelo menos dois dias de historico."

    taxas = await taxas_do_periodo(db, cliente, indexador, snapshots[0].date, snapshots[-1].date)
    if not taxas:
        return (
            snapshots,
            [],
            {},
            f"Nao foi possivel obter a serie do {NOMES[indexador]} no Banco Central agora.",
        )

    return snapshots, curva_equivalente(snapshots, taxas), taxas, None


def curva_rentabilidade(
    snapshots: list[PortfolioSnapshot], taxas: dict[date_type, Decimal]
) -> list[PontoRentabilidade]:
    """Rentabilidade acumulada da carteira e do indexador, ambas partindo de 0%.

    ## Por que NAO e `valor_mercado / custo_total - 1`

    Esse percentual ingenuo despenca a cada aporte, sem o mercado ter mexido:

        dia 1: investe 1.000, vale 1.100  ->  +10,0%
        dia 2: aporta 1.000, vale 2.100, custo 2.000  ->  +5,0%

    A rentabilidade "caiu pela metade" porque dinheiro novo entrou sem lucro e
    diluiu a conta. Um grafico assim mostraria quedas que nunca aconteceram --
    justamente nos dias em que a pessoa investiu mais.

    ## O que usamos: retorno ponderado pelo tempo (TWR)

        r[t] = (valor[t] - aporte[t]) / valor[t-1] - 1
        acumulado[t] = acumulado[t-1] * (1 + r[t])

    Subtrair o aporte do valor final isola o efeito do MERCADO daquele dia. No
    exemplo acima: (2.100 - 1.000) / 1.100 - 1 = 0%, e o acumulado segue +10%.

    E a medida que fundos reportam, e a unica comparavel com o CDI acumulado --
    que, por nao ter aporte, e simplesmente o produto das taxas diarias.
    """
    if not snapshots:
        return []

    curva = [PontoRentabilidade(snapshots[0].date, ZERO, ZERO if taxas else None)]

    acumulado_carteira = Decimal(1)
    acumulado_benchmark = Decimal(1)
    valor_anterior = snapshots[0].valor_mercado
    custo_anterior = snapshots[0].custo_total

    for ponto in snapshots[1:]:
        aporte = ponto.custo_total - custo_anterior

        # Valor anterior zero acontece: carteira zerada e depois reaberta. Nao ha
        # base para calcular retorno, entao o dia nao rende -- em vez de dividir
        # por zero ou inventar um numero.
        if valor_anterior > ZERO:
            retorno = (ponto.valor_mercado - aporte) / valor_anterior - 1
            acumulado_carteira *= 1 + retorno

        acumulado_benchmark *= 1 + taxas.get(ponto.date, ZERO)

        curva.append(
            PontoRentabilidade(
                date=ponto.date,
                carteira=acumulado_carteira - 1,
                benchmark=(acumulado_benchmark - 1) if taxas else None,
            )
        )

        valor_anterior = ponto.valor_mercado
        custo_anterior = ponto.custo_total

    return curva
