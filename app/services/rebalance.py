"""Do peso ideal para a ordem concreta.

A fronteira eficiente diz "40% em WEGE3". Isso nao e acionavel: ninguem compra
40%, compra 12 acoes. Este modulo faz a traducao.

## Dois modos, e por que os dois existem

**Com aporte** responde "dos meus proximos R$ 1.000, quanto vai para cada
ativo?". Nao vende nada -- logo nao gera imposto nem corretagem de venda, e nao
realiza prejuizo so para acertar um peso. E como a maioria das pessoas
rebalanceia de verdade.

**Completo** vende o que esta acima do peso para chegar exatamente no alvo.
Mais preciso, e honesto sobre o custo: venda de acao tem imposto sobre o ganho
e taxa de corretagem, e o plano mostra o quanto sera vendido para a pessoa
decidir se vale.

## Modulo puro

Entram posicoes, precos e pesos-alvo; saem ordens. Sem banco, sem HTTP, sem
ORM -- mesma escolha de `optimizer.py` e `position.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Protocol

from app.models.transaction import TransactionSide

ZERO = Decimal(0)
CENTAVOS = Decimal("0.01")


class PosicaoPrecificada(Protocol):
    """Uma posicao com preco de mercado. Sem preco nao ha o que rebalancear."""

    @property
    def ticker(self) -> str: ...
    @property
    def quantidade(self) -> Decimal: ...
    @property
    def preco_atual(self) -> Decimal | None: ...


@dataclass(frozen=True)
class Desvio:
    """Onde cada ativo esta em relacao ao alvo, ANTES de qualquer ordem.

    Existe separado das ordens de proposito: ver a distancia e util mesmo para
    quem nao vai rebalancear agora. Um desvio de 2 pontos raramente compensa a
    corretagem; um de 15 conta outra historia.
    """

    ticker: str
    peso_atual: Decimal
    peso_alvo: Decimal
    valor_atual: Decimal

    @property
    def diferenca(self) -> Decimal:
        """Positivo = acima do alvo. Em pontos de fracao (0.05 = 5 p.p.)."""
        return self.peso_atual - self.peso_alvo


@dataclass(frozen=True)
class Ordem:
    ticker: str
    side: TransactionSide
    quantidade: int
    preco: Decimal

    @property
    def valor(self) -> Decimal:
        return (self.quantidade * self.preco).quantize(CENTAVOS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Plano:
    ordens: list[Ordem]
    desvios: list[Desvio]
    sobra: Decimal
    sem_preco: list[str]

    @property
    def total_compras(self) -> Decimal:
        return sum((o.valor for o in self.ordens if o.side is TransactionSide.COMPRA), ZERO)

    @property
    def total_vendas(self) -> Decimal:
        return sum((o.valor for o in self.ordens if o.side is TransactionSide.VENDA), ZERO)


def _valores(posicoes: Sequence[PosicaoPrecificada]) -> tuple[dict[str, Decimal], list[str]]:
    """Valor de mercado por ticker, e quem ficou de fora por nao ter preco.

    Ativo sem cotacao e EXCLUIDO em vez de entrar pelo custo. No rebalanceamento
    o preco nao serve para exibir um total -- ele decide quantas acoes comprar.
    Chutar aqui vira ordem errada, com dinheiro real.
    """
    valores: dict[str, Decimal] = {}
    sem_preco: list[str] = []
    for p in posicoes:
        if p.preco_atual is None or p.preco_atual <= ZERO:
            sem_preco.append(p.ticker)
            continue
        valores[p.ticker] = p.quantidade * p.preco_atual
    return valores, sem_preco


def _em_acoes(valor: Decimal, preco: Decimal) -> int:
    """Quantas acoes INTEIRAS cabem no valor. Nao existe comprar 12,4 acoes."""
    if preco <= ZERO:
        return 0
    return int((valor / preco).to_integral_value(rounding=ROUND_DOWN))


def planejar(
    posicoes: Sequence[PosicaoPrecificada],
    alvos: Mapping[str, Decimal],
    *,
    aporte: Decimal = ZERO,
    permitir_venda: bool = False,
) -> Plano:
    """Traduz pesos-alvo em ordens de compra e venda.

    `alvos` mapeia ticker -> peso em FRACAO (0.40 = 40%). Ativo ausente do mapa
    tem alvo zero: no modo completo ele e zerado, no modo aporte apenas nao
    recebe dinheiro novo.
    """
    precos = {
        p.ticker: p.preco_atual
        for p in posicoes
        if p.preco_atual is not None and p.preco_atual > ZERO
    }
    valores, sem_preco = _valores(posicoes)
    patrimonio = sum(valores.values(), ZERO)

    # A base do calculo inclui o aporte: os pesos-alvo valem para a carteira
    # DEPOIS do aporte, senao o dinheiro novo entraria sem seguir o alvo.
    base = patrimonio + aporte

    tickers = sorted(set(valores) | set(alvos))
    desvios = [
        Desvio(
            ticker=t,
            peso_atual=(valores.get(t, ZERO) / patrimonio) if patrimonio > ZERO else ZERO,
            peso_alvo=Decimal(str(alvos.get(t, 0))),
            valor_atual=valores.get(t, ZERO),
        )
        for t in tickers
    ]

    if base <= ZERO:
        return Plano(ordens=[], desvios=desvios, sobra=aporte, sem_preco=sem_preco)

    faltam = {t: base * Decimal(str(alvos.get(t, 0))) - valores.get(t, ZERO) for t in tickers}

    ordens: list[Ordem] = []
    if permitir_venda:
        ordens.extend(_vender(faltam, precos))

    caixa = aporte + sum((o.valor for o in ordens), ZERO)
    compras, sobra = _comprar(faltam, precos, caixa)
    ordens.extend(compras)

    ordens.sort(key=lambda o: (o.side is TransactionSide.COMPRA, o.ticker))
    return Plano(ordens=ordens, desvios=desvios, sobra=sobra, sem_preco=sem_preco)


def _vender(faltam: Mapping[str, Decimal], precos: Mapping[str, Decimal]) -> list[Ordem]:
    """Vende o excedente de quem esta acima do alvo.

    Arredonda para BAIXO (`ROUND_DOWN` em `_em_acoes`): vender uma acao a mais
    do que o necessario deixaria o ativo abaixo do alvo, e o objetivo e chegar
    nele, nao passar dele.
    """
    ordens = []
    for ticker in sorted(faltam):
        excedente = -faltam[ticker]
        preco = precos.get(ticker)
        if preco is None or excedente <= ZERO:
            continue
        quantidade = _em_acoes(excedente, preco)
        if quantidade > 0:
            ordens.append(Ordem(ticker, TransactionSide.VENDA, quantidade, preco))
    return ordens


def _comprar(
    faltam: Mapping[str, Decimal], precos: Mapping[str, Decimal], caixa: Decimal
) -> tuple[list[Ordem], Decimal]:
    """Distribui o caixa entre quem esta abaixo do alvo.

    Duas passadas, e a segunda importa mais do que parece:

    1. Reparte proporcionalmente ao que falta e arredonda para BAIXO. Nunca
       gasta mais do que existe -- comprar a descoberto nao e uma opcao.
    2. Com a sobra do arredondamento, compra mais UMA acao de cada vez, sempre
       de quem esta mais longe do alvo. Sem essa passada, uma carteira de cinco
       ativos deixaria sistematicamente algumas centenas de reais parados, e o
       plano diria "sobrou R$ 380" quando dava para comprar mais coisa.
    """
    desejado = {t: v for t, v in faltam.items() if v > ZERO and t in precos}
    total = sum(desejado.values(), ZERO)
    if not desejado or total <= ZERO or caixa <= ZERO:
        return [], caixa

    # Se o caixa nao cobre tudo, reparte proporcionalmente ao tamanho da falta.
    fator = min(Decimal(1), caixa / total)

    quantidades: dict[str, int] = {}
    restante = caixa
    for ticker in sorted(desejado, key=lambda t: -desejado[t]):
        alvo_em_dinheiro = min(desejado[ticker] * fator, restante)
        quantidade = _em_acoes(alvo_em_dinheiro, precos[ticker])
        if quantidade > 0:
            quantidades[ticker] = quantidade
            restante -= quantidade * precos[ticker]

    # Segunda passada: gasta a sobra do arredondamento, uma acao por vez.
    while True:
        candidatos = [
            t
            for t in desejado
            if precos[t] <= restante and desejado[t] - quantidades.get(t, 0) * precos[t] > ZERO
        ]
        if not candidatos:
            break
        # Quem esta mais longe do alvo compra primeiro.
        escolhido = max(candidatos, key=lambda t: desejado[t] - quantidades.get(t, 0) * precos[t])
        quantidades[escolhido] = quantidades.get(escolhido, 0) + 1
        restante -= precos[escolhido]

    ordens = [
        Ordem(t, TransactionSide.COMPRA, q, precos[t]) for t, q in sorted(quantidades.items())
    ]
    return ordens, restante.quantize(CENTAVOS, rounding=ROUND_DOWN)
