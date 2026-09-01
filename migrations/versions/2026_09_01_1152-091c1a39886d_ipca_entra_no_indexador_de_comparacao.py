"""ipca entra no indexador de comparacao

Revision ID: 091c1a39886d
Revises: ed28216483b2
Create Date: 2026-09-01 11:52:38.750975

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "091c1a39886d"
down_revision: str | Sequence[str] | None = "ed28216483b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# O nome real no banco tem o prefixo "ck_benchmark_rates_" dobrado -- a
# convencao de nomenclatura do projeto (`ck_%(table_name)s_%(constraint_name)s`
# em app/models/base.py) envolve um nome que a propria coluna Enum(...) ja
# gera com esse prefixo. Nao e bonito, mas e o nome que existe de verdade no
# banco (conferido com `\d benchmark_rates` antes de escrever isto), e repetir
# aqui e o que faz `alembic check` continuar vendo o schema real como igual ao
# do modelo, em vez de inventar um nome novo que nunca vai bater.
#
# `op.f(...)` marca a string como um nome FINAL: "isto ja e o nome, nao
# reprocesse". Sem ele, tanto `drop_constraint` quanto `create_check_constraint`
# passam este nome PELA convencao de nomenclatura de novo -- o mesmo mecanismo
# que dobrou o prefixo na primeira vez -- e o resultado tripla, estoura o
# limite de 63 caracteres do Postgres e vira um nome truncado com hash que nao
# existe em lugar nenhum.
#
# `op.f()` so pode ser chamado DENTRO de `upgrade()`/`downgrade()`, nunca aqui
# no nivel do modulo: ele depende do contexto de `Operations` que o Alembic so
# ativa durante uma migracao de verdade. Guardar o RESULTADO de `op.f()` numa
# constante do modulo (como uma versao anterior deste arquivo fazia) quebra
# qualquer comando que so precise LER os scripts sem rodar migracao nenhuma --
# `alembic revision`, por exemplo, falhou com exatamente esse erro ao gerar a
# proxima migration. A constante guarda so a STRING; `op.f()` e chamado em cada
# call site, dentro das funcoes.
CONSTRAINT_NOME = "ck_benchmark_rates_ck_benchmark_rates_indexador"


def upgrade() -> None:
    """Permite 'ipca' na coluna que ate aqui so aceitava 'cdi' e 'selic'.

    O enum ganhou um membro nao muda a COLUNA sozinho: `native_enum=False`
    grava como VARCHAR livre, e quem restringe os valores aceitos e o CHECK
    criado a parte (ver a migration `ed28216483b2`). Sem trocar esse CHECK, a
    primeira tentativa de gravar uma taxa de IPCA falharia com uma violacao de
    constraint -- em producao, nao em teste, porque o teste local pode estar
    usando um banco recriado do zero a partir do modelo atual.
    """
    op.drop_constraint(op.f(CONSTRAINT_NOME), "benchmark_rates", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT_NOME), "benchmark_rates", "indexador IN ('cdi', 'selic', 'ipca')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT_NOME), "benchmark_rates", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT_NOME), "benchmark_rates", "indexador IN ('cdi', 'selic')"
    )
