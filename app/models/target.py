"""Alvo de preco por ativo: stop gain e stop loss.

## O que isto NAO e

Este app nao manda ordem para a B3 -- e um rastreador, nao uma corretora. Um
"stop" aqui nunca vende nada sozinho. Ele e um lembrete visual: a pessoa
define onde quer agir, e o app mostra quando aquele ponto foi cruzado. A
decisao de vender continua sendo manual, do jeito que sempre foi neste app.

## Por que dois tipos (percentual e preco)

Um alvo em PERCENTUAL acompanha o preco medio automaticamente: comprar mais do
mesmo ativo muda o preco medio, e "vender com 15% de lucro" continua fazendo
sentido sem reconfigurar nada.

Um alvo em PRECO FIXO e mais direto quando a decisao e sobre um numero
especifico ("saio se bater R$ 45"), independente de quanto custou. Nao
acompanha o preco medio -- se a pessoa comprar mais depois, o alvo continua
onde foi colocado, o que e exatamente o que se espera de um preco fixo.

Os dois tipos existem juntos porque sao duas perguntas diferentes, e forcar
uma resposta unica obrigaria a pessoa a converter uma na outra na cabeca toda
vez que o preco medio mudasse.

## Por que por (carteira, ativo), e nao um numero so na carteira

Stop de verdade e por posicao: a tolerancia a perda de PETR4 nao e a mesma de
uma small cap volatil. Um alvo unico sobre o patrimonio total esconderia essa
diferenca.
"""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, coluna_enum


class TipoAlvo(enum.StrEnum):
    PERCENTUAL = "percentual"
    PRECO = "preco"


class AssetTarget(Base, TimestampMixin):
    """Configuracao de stop gain / stop loss de um ativo numa carteira.

    `stop_loss_valor` guarda sempre uma MAGNITUDE positiva (0.08 = "8% abaixo
    do custo"), nunca um numero negativo -- o sinal esta no NOME do campo, nao
    no valor. Guardar -0.08 obrigaria toda leitura a lembrar de inverter o
    sinal, e um dia alguem esqueceria.
    """

    __tablename__ = "asset_targets"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    # Redundante com portfolio_id (todo portfolio tem um user_id), no mesmo
    # espirito de `Transaction.user_id`: permite varrer tudo de um usuario
    # (LGPD, exclusao de conta) sem JOIN, e sustenta um CASCADE direto.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # `name=` explicito nas duas: o mesmo enum `TipoAlvo` em duas colunas desta
    # tabela faria o SQLAlchemy nomear os dois CHECK constraints da mesma
    # forma (a partir do nome do enum, nao da coluna) -- ver `coluna_enum`.
    stop_gain_tipo: Mapped[TipoAlvo | None] = mapped_column(
        coluna_enum(TipoAlvo, length=10, name="tipoalvo_gain"), nullable=True
    )
    stop_gain_valor: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)

    stop_loss_tipo: Mapped[TipoAlvo | None] = mapped_column(
        coluna_enum(TipoAlvo, length=10, name="tipoalvo_loss"), nullable=True
    )
    stop_loss_valor: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)

    # Meta de ACUMULACAO: quanto a pessoa quer ter neste ativo, em reais.
    #
    # Mora na mesma tabela dos stops por dividir exatamente a mesma chave
    # (carteira + ativo) e o mesmo ciclo de vida -- uma tabela separada
    # duplicaria migration, servico, consulta e upsert para guardar uma
    # coluna. Mas e outra pergunta: o stop olha o PRECO ("quando sair"), a
    # meta olha o TAMANHO da posicao ("quanto ainda comprar"). Uma pode
    # existir sem a outra.
    meta_valor: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)

    __table_args__ = (
        # Tipo e valor andam juntos: um sem o outro e configuracao pela metade
        # que o dominio nao sabe interpretar (percentual de que? preco de
        # quanto?). O CHECK impede isso de entrar por qualquer via -- API,
        # script, UPDATE manual.
        CheckConstraint(
            "(stop_gain_tipo IS NULL) = (stop_gain_valor IS NULL)",
            name="stop_gain_tipo_e_valor_juntos",
        ),
        CheckConstraint(
            "(stop_loss_tipo IS NULL) = (stop_loss_valor IS NULL)",
            name="stop_loss_tipo_e_valor_juntos",
        ),
        CheckConstraint(
            "stop_gain_valor IS NULL OR stop_gain_valor > 0", name="stop_gain_valor_positivo"
        ),
        CheckConstraint(
            "stop_loss_valor IS NULL OR stop_loss_valor > 0", name="stop_loss_valor_positivo"
        ),
        CheckConstraint("meta_valor IS NULL OR meta_valor > 0", name="meta_valor_positiva"),
    )

    def __repr__(self) -> str:
        return f"<AssetTarget portfolio={self.portfolio_id} asset={self.asset_id}>"
