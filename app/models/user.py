"""Model de usuario."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    # "user" e palavra reservada no Postgres (SELECT user retorna o usuario da
    # sessao). Tabela no plural evita ter que escapar aspas em toda query manual.
    __tablename__ = "users"

    # 320 = limite do RFC 5321 (64 do local part + @ + 255 do dominio).
    # `unique=True` cria o indice que a rota de login usa para buscar por email --
    # sem ele, todo login vira sequential scan na tabela inteira.
    #
    # O email e sempre gravado em minusculas (normalizado no schema Pydantic, na
    # Etapa 2). Sem essa normalizacao, "Bernardo@x.com" e "bernardo@x.com" criam
    # duas contas distintas e a unicidade vira ficcao.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)

    # O nome deixa explicito que NUNCA existe senha em texto puro nesta coluna.
    # Guarda o hash argon2id completo (algoritmo + parametros + salt + digest),
    # que cabe folgado em 255 caracteres.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    # Desativar > deletar: apagar a linha derrubaria por cascade o historico de
    # transacoes, que e justamente o que o usuario nao quer perder. Alem disso
    # permite bloquear acesso na hora sem perder o rastro de auditoria.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Conta de demonstracao -----------------------------------------------
    #
    # A demo NAO e um caminho anonimo paralelo: e um usuario comum, com senha
    # inutilizavel, criado sob demanda. Essa escolha e o ponto central da
    # feature -- todo o isolamento do app vive em `get_current_user` e
    # `get_carteira`, e um caminho sem autenticacao seria uma SEGUNDA porta para
    # a camada de dados, sem nenhuma dessas garantias.
    #
    # Sendo um usuario como outro qualquer, nao ha codigo novo de escopo para
    # revisar, e os testes que ja protegem o isolamento passam a proteger a demo
    # de graca.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Quando a conta deixa de valer. Nulo em conta de verdade -- ela nao expira.
    #
    # Sem isto o banco cresceria sem limite: cada visitante que clicar em "ver
    # demonstracao" deixa uma conta, uma carteira, transacoes e snapshots.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def expirou(self) -> bool:
        """Conta de verdade nunca expira; demo expira na hora marcada."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    def __repr__(self) -> str:
        # Nao inclui o hash da senha. `repr()` de model aparece em log de erro e
        # em debugger com uma frequencia que surpreende.
        return f"<User id={self.id} email={self.email!r}>"
