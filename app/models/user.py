"""Model de usuario."""

from __future__ import annotations

from sqlalchemy import Boolean, String
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

    def __repr__(self) -> str:
        # Nao inclui o hash da senha. `repr()` de model aparece em log de erro e
        # em debugger com uma frequencia que surpreende.
        return f"<User id={self.id} email={self.email!r}>"
