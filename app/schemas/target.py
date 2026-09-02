"""Schemas do alvo de preco (stop gain / stop loss) por ativo."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from app.models.target import TipoAlvo
from app.services.target import StatusAlvo

# Solto o suficiente para nao atrapalhar preco fixo (R$ 1.000.000 e um preco
# valido) e percentual (1000% de alta e raro, mas nao impossivel numa small
# cap ao longo de anos). O limite real do stop loss percentual -- nao pode
# passar de 100% -- e checado a parte, porque depende do TIPO.
ValorDoAlvo = Annotated[Decimal, Field(gt=0, le=Decimal("1e9"), decimal_places=6)]


class TargetSet(BaseModel):
    """Corpo do `PUT /portfolio/targets/{ticker}`.

    Cada lado (gain, loss) e opcional e independente; e cada lado escolhe o
    proprio tipo. Mandar so `stop_gain_*` mantem (ou remove, se omitido) o
    stop_loss como estava? Nao -- este endpoint substitui os DOIS lados de
    uma vez. Ver o motivo em `target_service.definir`.
    """

    stop_gain_tipo: TipoAlvo | None = None
    stop_gain_valor: ValorDoAlvo | None = None
    stop_loss_tipo: TipoAlvo | None = None
    stop_loss_valor: ValorDoAlvo | None = None

    # Meta de acumulacao: quanto se quer ter neste ativo, em reais. Nao tem
    # "tipo" -- sempre um valor absoluto, porque um percentual de acumulacao
    # precisaria de uma base ("percentual de que?") que so existiria se a meta
    # total da carteira fosse obrigatoria, e ela nao e.
    meta_valor: Annotated[Decimal, Field(gt=0, le=Decimal("1e12"), decimal_places=6)] | None = None

    @model_validator(mode="after")
    def _tipo_e_valor_andam_juntos(self) -> TargetSet:
        """Tipo sem valor ou valor sem tipo e configuracao que o dominio nao
        sabe interpretar -- percentual de que, ou preco de quanto?"""
        if (self.stop_gain_tipo is None) != (self.stop_gain_valor is None):
            raise ValueError("informe tipo e valor do stop gain juntos, ou nenhum dos dois")
        if (self.stop_loss_tipo is None) != (self.stop_loss_valor is None):
            raise ValueError("informe tipo e valor do stop loss juntos, ou nenhum dos dois")
        return self

    @model_validator(mode="after")
    def _stop_loss_percentual_nao_passa_de_cem_por_cento(self) -> TargetSet:
        """Uma posicao nao cai mais que 100% do que custou -- e diferente do
        stop gain, que nao tem teto natural (uma acao pode multiplicar por 10).
        """
        if (
            self.stop_loss_tipo is TipoAlvo.PERCENTUAL
            and self.stop_loss_valor is not None
            and self.stop_loss_valor > 1
        ):
            raise ValueError("stop loss percentual nao pode passar de 100% (a posicao zeraria)")
        return self


class PortfolioGoalSet(BaseModel):
    """Corpo do `PUT /portfolio/goal`. `valor: null` remove a meta."""

    valor: Annotated[Decimal, Field(gt=0, le=Decimal("1e12"), decimal_places=6)] | None = None


class MetaResumo(BaseModel):
    """Progresso em direcao a uma meta de acumulacao, ja calculado.

    `progresso` nao e limitado a 1: quem passou da meta ve 1,2 (120%), e a
    tela decide como mostrar isso. Travar em 100% no servidor apagaria a
    informacao de QUANTO se passou.
    """

    meta: Decimal
    atual: Decimal
    falta: Decimal
    progresso: Decimal
    atingida: bool


class AlvoResumo(BaseModel):
    """Estado do alvo de um ativo, embutido em cada linha do resumo da
    carteira -- para a tela nao precisar de uma segunda chamada por ativo."""

    stop_gain_tipo: TipoAlvo | None = None
    stop_gain_valor: Decimal | None = None
    stop_loss_tipo: TipoAlvo | None = None
    stop_loss_valor: Decimal | None = None
    status: StatusAlvo = StatusAlvo.SEM_ALVO

    # `None` quando nao ha meta definida para o ativo -- distinto de uma meta
    # de valor zero, que o CHECK do banco nem deixa existir.
    meta: MetaResumo | None = None


class MetaDaCarteira(BaseModel):
    """Meta de patrimonio da carteira inteira.

    `soma_das_metas` e `nao_distribuido` existem por causa da escolha de ter
    as duas coisas: a meta geral E as por ativo. Sem eles, a pessoa definiria
    R$ 100 mil na carteira, R$ 20 mil em tres papeis, e nada na tela diria
    que R$ 40 mil do objetivo ainda nao tem destino.
    """

    progresso: MetaResumo | None = None
    soma_das_metas: Decimal
    nao_distribuido: Decimal | None = None
