"""Comparacao da carteira contra CDI, Selic, IPCA ou Ibovespa.

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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from functools import partial

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.bcb import BcbClient
from app.clients.ibov import IbovClient
from app.models.benchmark import BenchmarkRate, Indexador
from app.models.snapshot import PortfolioSnapshot
from app.services.metrics import PREGOES_POR_ANO

# O formato que qualquer fornecedor de taxas precisa produzir: dado um
# periodo, uma taxa diaria por data. `BcbClient.taxas` (com o indexador ja
# fixado via `partial`) e `IbovClient.variacoes_diarias` tem assinaturas
# diferentes uma da outra, mas ambas encaixam aqui -- e e essa forma comum que
# deixa `taxas_do_periodo` sem precisar saber de qual fornecedor veio o
# numero.
BuscarTaxas = Callable[[date_type, date_type], Awaitable[dict[date_type, Decimal]]]

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
    indexador: Indexador,
    desde: date_type,
    ate: date_type,
    *,
    buscar: BuscarTaxas,
) -> dict[date_type, Decimal]:
    """Le do banco e busca no fornecedor so o que faltar.

    `buscar` e quem sabe de onde vem o numero (BCB para CDI/Selic/IPCA, Yahoo
    para Ibovespa) -- esta funcao so sabe cachear. Mesma logica do cache de
    cotacoes, com uma diferenca importante: taxa de um dia PASSADO nao muda.
    Uma vez gravado, o CDI (ou o Ibovespa) de um dia esta gravado para sempre
    -- entao nao ha TTL aqui, so preenchimento das lacunas.

    A verificacao de lacuna e por COBERTURA das pontas, nao por contagem: nem
    BCB nem B3 publicam em fim de semana e feriado, entao "faltam dias" e o
    estado normal de qualquer intervalo.
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

    novas = await buscar(desde, ate)
    if not novas:
        # O fornecedor fora do ar nao pode derrubar o grafico da carteira:
        # devolvemos o que ja tinhamos, mesmo incompleto.
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


# A taxa livre de risco e o CDI MAIS RECENTE, lido da mesma tabela que o
# grafico de evolucao ja preenche.
#
# Ate `CDI_RECENTE_DIAS` a ultima taxa gravada vale como "a de hoje" sem
# perguntar ao BCB de novo: cobre fim de semana com feriado emendado (de sexta a
# quarta-feira de cinzas sao 5 dias). Fora disso busca-se o ultimo mes; e, se o
# BCB nao responder, uma taxa de ate `CDI_TOLERADO_DIAS` ainda serve -- com a
# data a vista na tela, porque o CDI muda devagar e a data denuncia o atraso.
CDI_RECENTE_DIAS = 7
CDI_BUSCA_DIAS = 30
CDI_TOLERADO_DIAS = 45


@dataclass(frozen=True)
class TaxaLivreRisco:
    """A taxa anual usada no Sharpe e no Black-Litterman, e de onde ela veio.

    A origem viaja junto porque o numero sozinho nao se explica: 13,65% "do
    CDI de ontem" e 10% "do arquivo de configuracao" pedem leituras diferentes,
    e a segunda tem que aparecer como aviso.
    """

    valor: float
    fonte: str
    data: date_type | None
    """Dia do CDI usado. None quando o valor veio da configuracao."""


def anualizar_taxa_diaria(taxa: Decimal | float) -> float:
    """(1 + taxa do dia) ^ 252 - 1.

    O CDI e publicado como taxa DIARIA e compoe por dia util, entao o ano tem
    252 periodos -- a mesma convencao das demais metricas. Multiplicar por 252
    em vez de elevar subestimaria a taxa em quase um ponto com o CDI a 14%.
    """
    return float((1.0 + float(taxa)) ** PREGOES_POR_ANO - 1.0)


async def _ultimo_cdi(db: AsyncSession, desde: date_type) -> BenchmarkRate | None:
    return (
        await db.execute(
            select(BenchmarkRate)
            .where(BenchmarkRate.indexador == Indexador.CDI, BenchmarkRate.date >= desde)
            .order_by(BenchmarkRate.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def taxa_livre_risco(
    db: AsyncSession, bcb: BcbClient, *, hoje: date_type, reserva: float
) -> TaxaLivreRisco:
    """CDI anualizado mais recente; `reserva` (o RISK_FREE_RATE) so se nao houver.

    A versao anterior lia so o `.env`, ajustado a mao "conforme o COPOM" -- e
    ficou em 10% com a Selic em 13,75%. Esse numero entra no Sharpe, na
    conversao das opinioes absolutas e no retorno total de cada ativo; um ponto
    e meio de erro nele desloca a fronteira inteira sem estourar nada.
    """
    linha = await _ultimo_cdi(db, hoje - timedelta(days=CDI_RECENTE_DIAS))
    if linha is None:
        await taxas_do_periodo(
            db,
            Indexador.CDI,
            hoje - timedelta(days=CDI_BUSCA_DIAS),
            hoje,
            buscar=partial(bcb.taxas, Indexador.CDI),
        )
        linha = await _ultimo_cdi(db, hoje - timedelta(days=CDI_TOLERADO_DIAS))

    if linha is None:
        logger.warning("[taxa_livre_risco] sem CDI recente; usando a reserva configurada")
        return TaxaLivreRisco(
            valor=reserva,
            fonte="valor de reserva da configuração (CDI do Banco Central indisponível)",
            data=None,
        )
    return TaxaLivreRisco(
        valor=anualizar_taxa_diaria(linha.rate),
        fonte=f"CDI de {linha.date:%d/%m/%Y}, Banco Central",
        data=linha.date,
    )


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


NOMES = {
    Indexador.CDI: "CDI",
    Indexador.SELIC: "Selic",
    Indexador.IPCA: "IPCA",
    Indexador.IBOV: "Ibovespa",
}

# Usado so na mensagem de erro ("nao foi possivel obter a serie X em Y agora"):
# CDI/Selic/IPCA vem do Banco Central, Ibovespa do Yahoo Finance -- dizer
# "Banco Central" para os quatro seria simplesmente falso para o Ibovespa.
FONTES = {
    Indexador.CDI: "Banco Central",
    Indexador.SELIC: "Banco Central",
    Indexador.IPCA: "Banco Central",
    Indexador.IBOV: "Yahoo Finance",
}


async def evolucao_comparada(
    db: AsyncSession,
    bcb: BcbClient,
    ibov: IbovClient,
    portfolio_id: uuid.UUID,
    indexador: Indexador | None,
    *,
    desde: date_type | None = None,
    ate: date_type | None = None,
    limite: int,
) -> tuple[list[PortfolioSnapshot], list[PontoBenchmark], dict[date_type, Decimal], str | None]:
    """Snapshots em ordem cronologica + a curva equivalente do indexador.

    `indexador=None` significa "sem comparacao": devolve o historico e curva
    vazia. Nao e um caso de erro, e uma escolha do usuario.
    """
    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == portfolio_id)
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

    if indexador is None:
        # Sair ANTES da checagem de tamanho: quem nao pediu comparacao nao pode
        # receber "a comparacao precisa de dois dias" como explicacao. E poupa
        # uma ida ao BCB para buscar taxas que ninguem usaria.
        return snapshots, [], {}, None

    if len(snapshots) < 2:
        return snapshots, [], {}, "A comparacao precisa de pelo menos dois dias de historico."

    # `ibov.variacoes_diarias` nao recebe `indexador` (so existe um indice);
    # `partial` fixa esse parametro em `bcb.taxas` para que os dois encaixem na
    # mesma forma (`BuscarTaxas`) e `taxas_do_periodo` nao precise de um `if`
    # para saber qual fornecedor chamar.
    buscar: BuscarTaxas = (
        ibov.variacoes_diarias if indexador is Indexador.IBOV else partial(bcb.taxas, indexador)
    )
    taxas = await taxas_do_periodo(
        db, indexador, snapshots[0].date, snapshots[-1].date, buscar=buscar
    )
    if not taxas:
        return (
            snapshots,
            [],
            {},
            f"Nao foi possivel obter a serie do {NOMES[indexador]} em {FONTES[indexador]} agora.",
        )

    return snapshots, curva_equivalente(snapshots, taxas), taxas, None


def curva_rentabilidade(
    snapshots: list[PortfolioSnapshot],
    taxas: dict[date_type, Decimal],
    proventos: dict[date_type, Decimal] | None = None,
) -> list[PontoRentabilidade]:
    """Rentabilidade acumulada da carteira e do indexador, ambas partindo de 0%.

    `proventos` mapeia data-com -> valor liquido recebido naquele dia. Opcional
    para nao quebrar quem chama sem ele, mas omitir significa medir apenas
    valorizacao, e nao retorno total.

    ## Por que NAO e `valor_mercado / custo_total - 1`

    Esse percentual ingenuo despenca a cada aporte, sem o mercado ter mexido:

        dia 1: investe 1.000, vale 1.100  ->  +10,0%
        dia 2: aporta 1.000, vale 2.100, custo 2.000  ->  +5,0%

    A rentabilidade "caiu pela metade" porque dinheiro novo entrou sem lucro e
    diluiu a conta. Um grafico assim mostraria quedas que nunca aconteceram --
    justamente nos dias em que a pessoa investiu mais.

    ## O que usamos: retorno ponderado pelo tempo (TWR)

        r[t] = (valor[t] + provento[t] - aporte[t]) / valor[t-1] - 1
        acumulado[t] = acumulado[t-1] * (1 + r[t])

    Subtrair o aporte do valor final isola o efeito do MERCADO daquele dia. No
    exemplo acima: (2.100 - 1.000) / 1.100 - 1 = 0%, e o acumulado segue +10%.

    E a medida que fundos reportam, e a unica comparavel com o CDI acumulado --
    que, por nao ter aporte, e simplesmente o produto das taxas diarias.
    """
    if not snapshots:
        return []

    proventos = proventos or {}
    curva = [PontoRentabilidade(snapshots[0].date, ZERO, ZERO if taxas else None)]

    acumulado_carteira = Decimal(1)
    acumulado_benchmark = Decimal(1)
    valor_anterior = snapshots[0].valor_mercado
    custo_anterior = snapshots[0].custo_total

    for ponto in snapshots[1:]:
        aporte = ponto.custo_total - custo_anterior

        # Provento recebido no dia SOMA no numerador.
        #
        # Na data-com o preco cai aproximadamente o valor do provento -- a
        # empresa distribuiu caixa, entao vale menos. O snapshot registra essa
        # queda, mas o dinheiro nao evaporou: saiu da cotacao e entrou na conta.
        # Sem somar de volta, a carteira aparece perdendo exatamente o que
        # ganhou, e um bom pagador de dividendo (TAEE11, ITUB4) fica com
        # retorno cronicamente subestimado. Isto e retorno TOTAL, que e o que
        # fundos reportam e a unica coisa comparavel com o CDI acumulado.
        recebido = proventos.get(ponto.date, ZERO)

        # Valor anterior zero acontece: carteira zerada e depois reaberta. Nao ha
        # base para calcular retorno, entao o dia nao rende -- em vez de dividir
        # por zero ou inventar um numero.
        if valor_anterior > ZERO:
            retorno = (ponto.valor_mercado + recebido - aporte) / valor_anterior - 1
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
