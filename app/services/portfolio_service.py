"""Resumo da carteira: posicao + cotacao = valor de mercado."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.base import ProvedorDeCotacoes
from app.schemas.portfolio import PortfolioSummary, PortfolioTotals, PositionSummary
from app.services import quote_service, transaction_service

ZERO = Decimal(0)
CEM = Decimal(100)


async def resumo(
    db: AsyncSession,
    provedor: ProvedorDeCotacoes,
    portfolio_id: uuid.UUID,
    *,
    ttl_segundos: int,
) -> PortfolioSummary:
    """Consolida a carteira com preco de mercado.

    Posicoes zeradas sao excluidas do resumo de MERCADO -- nao ha o que valorizar
    numa posicao de quantidade zero, e buscar cotacao para ela gastaria cota do
    fornecedor a toa. O resultado realizado delas continua somando no total,
    porque e dinheiro que o usuario de fato ganhou ou perdeu.
    """
    posicoes = await transaction_service.posicoes(db, portfolio_id)
    abertas = [p for p in posicoes if not p.esta_zerada]

    cotacoes = await quote_service.cotacoes_atuais(
        db, provedor, [p.ticker for p in abertas], ttl_segundos=ttl_segundos
    )

    linhas: list[PositionSummary] = []
    sem_cotacao: list[str] = []
    total_custo = total_mercado = ZERO

    for posicao in abertas:
        linha = PositionSummary(
            ticker=posicao.ticker,
            quantidade=posicao.quantidade,
            preco_medio=posicao.preco_medio,
            custo_total=posicao.custo_total,
            resultado_realizado=posicao.resultado_realizado,
        )
        total_custo += posicao.custo_total

        cotacao = cotacoes.get(posicao.ticker)
        if cotacao is None:
            sem_cotacao.append(posicao.ticker)
            # Sem preco, o ativo entra no total de mercado pelo CUSTO. Entrar
            # como zero faria a carteira parecer ter derretido; omiti-lo faria os
            # totais nao fecharem com a soma das linhas.
            total_mercado += posicao.custo_total
            linhas.append(linha)
            continue

        valor = posicao.quantidade * cotacao.preco
        linha.preco_atual = cotacao.preco
        linha.valor_mercado = valor
        linha.resultado_nao_realizado = valor - posicao.custo_total
        # Guarda de divisao por zero: custo zero acontece de verdade -- ativo
        # recebido em bonificacao, ou posicao reaberta apos zerar.
        if posicao.custo_total != ZERO:
            linha.variacao_percentual = (valor - posicao.custo_total) / posicao.custo_total * CEM
        linha.cotacao_em = cotacao.obtida_em
        linha.cotacao_fonte = cotacao.fonte

        total_mercado += valor
        linhas.append(linha)

    realizado = sum((p.resultado_realizado for p in posicoes), ZERO)
    nao_realizado = total_mercado - total_custo

    return PortfolioSummary(
        positions=linhas,
        totals=PortfolioTotals(
            custo_total=total_custo,
            valor_mercado=total_mercado,
            resultado_nao_realizado=nao_realizado,
            resultado_realizado=realizado,
            variacao_percentual=(
                nao_realizado / total_custo * CEM if total_custo != ZERO else None
            ),
        ),
        sem_cotacao=sem_cotacao,
    )
