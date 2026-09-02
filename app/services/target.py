"""Avalia se o preco atual bateu o stop gain ou o stop loss de um ativo.

Modulo puro -- mesma familia de `optimizer.py`, `rebalance.py`, `simulation.py`:
sem banco, sem ORM, sem HTTP. Entra o alvo configurado, o preco medio e a
cotacao atual; sai um veredito. Testavel com uma calculadora, sem subir nada.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.models.target import TipoAlvo


class StatusAlvo(enum.StrEnum):
    """O que mostrar na tela para esta posicao."""

    SEM_ALVO = "sem_alvo"
    DENTRO = "dentro"
    GAIN_ATINGIDO = "gain_atingido"
    LOSS_ATINGIDO = "loss_atingido"


class AlvoLike(Protocol):
    """O que a avaliacao precisa do model, sem depender dele.

    Mesmo papel de `PosicaoPrecificada` em `rebalance.py`: o modulo puro
    recebe qualquer objeto com esta forma, ORM ou nao, e o teste nao precisa
    de banco para exercitar a regra.
    """

    @property
    def stop_gain_tipo(self) -> TipoAlvo | None: ...
    @property
    def stop_gain_valor(self) -> Decimal | None: ...
    @property
    def stop_loss_tipo(self) -> TipoAlvo | None: ...
    @property
    def stop_loss_valor(self) -> Decimal | None: ...


@dataclass(frozen=True)
class Alvo:
    """Implementacao concreta do Protocol acima, para uso fora do ORM (testes,
    e o valor devolvido para a API quando nao ha alvo configurado)."""

    stop_gain_tipo: TipoAlvo | None = None
    stop_gain_valor: Decimal | None = None
    stop_loss_tipo: TipoAlvo | None = None
    stop_loss_valor: Decimal | None = None


def _limite(preco_medio: Decimal, tipo: TipoAlvo, valor: Decimal, *, sinal: int) -> Decimal:
    """O preco que dispara o alvo.

    `tipo=PRECO`: o valor JA E o preco-limite, o preco medio nao entra na conta
    -- e o ponto de um alvo fixo, ele nao acompanha o custo.

    `tipo=PERCENTUAL`: o limite e relativo ao preco medio. `sinal` decide a
    direcao -- +1 para cima (stop gain), -1 para baixo (stop loss). `valor` e
    sempre uma magnitude positiva (0.08 = 8%); o sinal do deslocamento vem
    daqui, nunca do valor guardado.
    """
    if tipo is TipoAlvo.PRECO:
        return valor
    return preco_medio * (Decimal(1) + sinal * valor)


def avaliar(
    alvo: AlvoLike | None, *, preco_medio: Decimal, preco_atual: Decimal | None
) -> StatusAlvo:
    """Devolve o status do alvo para esta posicao, agora.

    Sem cotacao (`preco_atual is None`), o veredito e DENTRO, nunca atingido:
    nao ha preco para comparar, e a alternativa -- presumir que o alvo bateu --
    e o tipo de falso positivo que faz a pessoa vender no momento errado
    confiando num numero que o app inventou.

    Gain e loss sao checados em sequencia, gain primeiro. Na pratica os dois
    nunca disparam juntos com valores configurados de forma sensata (um esta
    acima do custo, o outro abaixo) -- a ordem so importa se alguem configurar
    um stop loss acima do preco medio por engano, e nesse caso mostrar "bateu o
    gain" e a leitura mais honesta do que aconteceu com o preco.
    """
    if alvo is None or (alvo.stop_gain_tipo is None and alvo.stop_loss_tipo is None):
        return StatusAlvo.SEM_ALVO
    if preco_atual is None:
        return StatusAlvo.DENTRO

    # `and valor is not None` narra para o type checker o que o CHECK
    # constraint do banco ja garante (tipo e valor andam juntos) -- sem
    # precisar de `assert` para provar uma invariante que a propria condicao
    # ja verifica.
    if alvo.stop_gain_tipo is not None and alvo.stop_gain_valor is not None:
        limite = _limite(preco_medio, alvo.stop_gain_tipo, alvo.stop_gain_valor, sinal=1)
        if preco_atual >= limite:
            return StatusAlvo.GAIN_ATINGIDO
    if alvo.stop_loss_tipo is not None and alvo.stop_loss_valor is not None:
        limite = _limite(preco_medio, alvo.stop_loss_tipo, alvo.stop_loss_valor, sinal=-1)
        if preco_atual <= limite:
            return StatusAlvo.LOSS_ATINGIDO

    return StatusAlvo.DENTRO
