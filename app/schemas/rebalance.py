"""Schemas do plano de rebalanceamento."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, field_serializer, model_validator

from app.models.transaction import TransactionSide
from app.schemas.transaction import _dinheiro, _enxuto


class RebalanceRequest(BaseModel):
    """Os pesos vem do cliente, e nao sao recalculados aqui.

    A tela ja pediu a otimizacao e mostrou a fronteira; mandar os pesos de volta
    deixa o usuario rebalancear para a carteira que ELE escolheu no grafico --
    minima variancia, maximo Sharpe, ou qualquer ponto da curva. Recalcular no
    servidor entregaria uma carteira possivelmente diferente da que ele viu.
    """

    pesos: dict[str, Decimal] = Field(description="ticker -> peso em fracao (0.40 = 40%)")
    aporte: Annotated[Decimal, Field(ge=0, le=Decimal("1e9"))] = Decimal(0)
    permitir_venda: bool = Field(
        default=False,
        description="False: so distribui o aporte. True: vende o que esta acima do alvo.",
    )

    @model_validator(mode="after")
    def _valida(self) -> RebalanceRequest:
        if not self.pesos:
            raise ValueError("informe ao menos um peso")
        if any(p < 0 for p in self.pesos.values()):
            raise ValueError("peso negativo nao existe: isto nao opera vendido")
        total = sum(self.pesos.values())
        # Tolerancia de 1%: os pesos vem de uma otimizacao numerica e somam
        # 0.999... com frequencia. Exigir soma exata recusaria pedidos corretos.
        if not (Decimal("0.99") <= total <= Decimal("1.01")):
            raise ValueError(f"os pesos precisam somar 100%; somaram {total * 100:.1f}%")
        if not self.permitir_venda and self.aporte <= 0:
            raise ValueError("sem venda e sem aporte nao ha o que rebalancear")
        return self


class DesvioRead(BaseModel):
    ticker: str
    peso_atual: Decimal
    peso_alvo: Decimal
    diferenca: Decimal = Field(description="Positivo = acima do alvo, em fracao")
    valor_atual: Decimal

    @field_serializer("peso_atual", "peso_alvo", "diferenca")
    def _s_peso(self, v: Decimal) -> Decimal:
        return v.quantize(Decimal("0.000001"))

    @field_serializer("valor_atual")
    def _s_valor(self, v: Decimal) -> Decimal:
        return _dinheiro(v)


class OrdemRead(BaseModel):
    ticker: str
    side: TransactionSide
    quantidade: int
    preco: Decimal
    valor: Decimal

    @field_serializer("preco")
    def _s_preco(self, v: Decimal) -> Decimal:
        return _enxuto(v)

    @field_serializer("valor")
    def _s_valor(self, v: Decimal) -> Decimal:
        return _dinheiro(v)


class RebalanceResponse(BaseModel):
    ordens: list[OrdemRead]
    desvios: list[DesvioRead]
    total_compras: Decimal
    total_vendas: Decimal
    sobra: Decimal = Field(description="Dinheiro que nao coube em acoes inteiras")
    # Ativo sem cotacao fica FORA do plano: sem preco nao da para decidir
    # quantas acoes comprar, e chutar aqui viraria ordem errada com dinheiro
    # real. A tela precisa avisar em vez de omitir em silencio.
    sem_preco: list[str] = Field(default_factory=list)

    @field_serializer("total_compras", "total_vendas", "sobra")
    def _s_dinheiro(self, v: Decimal) -> Decimal:
        return _dinheiro(v)
