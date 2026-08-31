"""Regras de negocio de cadastro e autenticacao."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password, verify_password_dummy
from app.models.user import User
from app.schemas.user import UserCreate
from app.services.exceptions import (
    ContaInativaError,
    CredenciaisInvalidasError,
    EmailJaCadastradoError,
)


async def criar_usuario(db: AsyncSession, dados: UserCreate) -> User:
    """Cria o usuario com a senha ja hasheada.

    Nao fazemos "SELECT para ver se o email existe, depois INSERT". Esse padrao e
    uma corrida (TOCTOU): dois requests simultaneos passam os dois no SELECT e um
    dos INSERTs estoura -- em produção, com 500. Aqui a fonte da verdade e a
    constraint UNIQUE do banco, que e atomica por definicao; o INSERT e tentado e
    o IntegrityError e traduzido.
    """
    usuario = User(email=dados.email, hashed_password=hash_password(dados.password))
    db.add(usuario)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise EmailJaCadastradoError(dados.email) from exc
    await db.refresh(usuario)
    return usuario


async def autenticar(db: AsyncSession, email: str, senha: str) -> User:
    """Valida credenciais em tempo constante e devolve o usuario.

    Tres cuidados, nesta ordem:

    1. Email inexistente ainda paga o custo de um argon2 (`verify_password_dummy`).
       Sem isso o login vira um oraculo: resposta em 1ms = conta nao existe,
       resposta em 50ms = conta existe com senha errada. A mensagem identica nao
       adianta nada se o relogio denuncia.

    2. Senha errada e email inexistente levantam a MESMA excecao. Quem chama nao
       tem como diferenciar nem por acidente.

    3. `verify_and_update` regrava o hash quando os parametros do argon2 mudam.
       E a unica janela em que a senha em texto puro existe -- se nao aproveitar
       agora, contas antigas ficam presas ao custo antigo para sempre.
    """
    usuario = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if usuario is None:
        verify_password_dummy(senha)
        raise CredenciaisInvalidasError

    valida, novo_hash = verify_password(senha, usuario.hashed_password)
    if not valida:
        raise CredenciaisInvalidasError

    # A checagem de conta inativa vem DEPOIS da senha: se viesse antes, um
    # atacante descobriria contas desativadas sem saber a senha delas.
    if not usuario.is_active:
        raise ContaInativaError

    if novo_hash is not None:
        usuario.hashed_password = novo_hash
        await db.commit()

    return usuario
