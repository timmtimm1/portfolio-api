"""Hash de senha e emissao/validacao de JWT.

Este modulo nao conhece banco, nem FastAPI, nem models. E proposital: funcoes
puras sao as unicas que da para testar exaustivamente, e criptografia e
exatamente o codigo que voce quer testar exaustivamente.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import get_settings

ALGORITHM = "HS256"

TokenType = Literal["access", "refresh"]

# Argon2id -- vencedor da Password Hashing Competition e a recomendacao atual da
# OWASP. Por que nao as alternativas comuns:
#   - SHA256/MD5: rapidos demais. Uma GPU testa bilhoes por segundo.
#   - passlib + bcrypt: bcrypt ainda e aceitavel, mas passlib esta sem manutencao
#     (o tutorial oficial do FastAPI ainda o mostra) e bcrypt trunca a senha em
#     72 bytes silenciosamente.
# Argon2 e caro de proposito -- em memoria, nao so em CPU, o que tira a vantagem
# de quem ataca com GPU/ASIC.
_password_hash = PasswordHash((Argon2Hasher(),))

# Hash descartavel usado para gastar o mesmo tempo quando o email nao existe.
# Ver `verify_password_dummy` -- e a defesa contra enumeracao por timing.
_DUMMY_HASH = _password_hash.hash(secrets.token_urlsafe(32))


def hash_password(senha: str) -> str:
    """Devolve o hash argon2id completo (algoritmo, parametros, salt e digest).

    O salt e gerado por senha e vai embutido na string -- por isso duas contas com
    a mesma senha produzem hashes diferentes, e por isso rainbow table nao serve
    de nada aqui.
    """
    return _password_hash.hash(senha)


def verify_password(senha: str, hash_armazenado: str) -> tuple[bool, str | None]:
    """Verifica a senha e diz se o hash precisa ser regravado.

    O segundo elemento vem preenchido quando o hash foi feito com parametros
    antigos (custo menor, algoritmo anterior). Regravar no login e o unico jeito
    de fortalecer hashes de contas antigas sem pedir que todo mundo troque a
    senha -- so a senha em texto puro, disponivel apenas nesse instante, permite
    gerar o hash novo.
    """
    return _password_hash.verify_and_update(senha, hash_armazenado)


def verify_password_dummy(senha: str) -> None:
    """Gasta o mesmo tempo de um argon2 real, e descarta o resultado.

    Sem isso, "email inexistente" responde em ~1ms e "senha errada" em ~50ms.
    Essa diferenca e mensuravel pela rede e transforma o login num oraculo de
    quem tem conta no sistema -- mesmo com a mensagem de erro sendo identica nos
    dois casos. Chamada no caminho em que o usuario nao foi encontrado.
    """
    _password_hash.verify(senha, _DUMMY_HASH)


def create_token(
    subject: uuid.UUID,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Emite um JWT assinado. Devolve (token, jti).

    Claims deliberadas:
      sub  -- id do usuario (string; o JWT exige)
      typ  -- "access" ou "refresh". SEM isso, um refresh token (que vive 30 dias)
              seria aceito como token de acesso, anulando a expiracao curta do
              access token. E uma confusao de tipo com consequencia direta.
      exp  -- expiracao. Um JWT sem exp e valido para sempre; se vazar, nao ha
              como revogar sem trocar a SECRET_KEY de todo mundo.
      iat  -- emitido em. Permite invalidar em massa tudo anterior a um incidente.
      jti  -- id unico do token. E o que torna a revogacao individual possivel
              (usado na Etapa 3, na rotacao de refresh token).

    Nao existe dado sensivel nas claims: o payload de um JWT e apenas base64, nao
    e criptografado. Qualquer um com o token le o conteudo. A assinatura garante
    que ninguem *alterou* -- nao que ninguem *leu*.
    """
    agora = datetime.now(UTC)
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "exp": agora + expires_delta,
        "iat": agora,
        "jti": jti,
        **(extra_claims or {}),
    }
    token = jwt.encode(
        payload,
        get_settings().SECRET_KEY.get_secret_value(),
        algorithm=ALGORITHM,
    )
    return token, jti


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Valida assinatura, expiracao e tipo. Levanta `jwt.InvalidTokenError`.

    `algorithms=[ALGORITHM]` e uma lista fechada de proposito: aceitar o algoritmo
    que vem no cabecalho do proprio token e a vulnerabilidade classica de JWT --
    o atacante manda `alg: none` (ou troca RS256 por HS256 usando a chave publica
    como segredo) e forja qualquer identidade.

    A checagem de `typ` roda depois da assinatura: so confiamos no conteudo
    depois de provar que o token nao foi adulterado.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        get_settings().SECRET_KEY.get_secret_value(),
        algorithms=[ALGORITHM],
        options={"require": ["exp", "iat", "sub", "jti", "typ"]},
    )
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError(f"tipo de token invalido: esperado {expected_type}")
    return payload


# --- Refresh token (valor opaco, nao JWT -- ver app/models/refresh_token.py) ---

# 48 bytes = 384 bits de entropia. Bem acima dos 128 bits que a OWASP pede para
# um identificador de sessao; adivinhar por forca bruta e fisicamente inviavel.
REFRESH_TOKEN_BYTES = 48


def generate_refresh_token() -> tuple[str, str]:
    """Gera (token em texto puro, hash para o banco).

    `secrets` usa a fonte de aleatoriedade do sistema operacional. Nunca use
    `random` para isso: o Mersenne Twister e reproduzivel -- com algumas saidas
    observadas da-se para prever todas as proximas.

    O texto puro so existe nesta funcao e na resposta HTTP. O banco recebe apenas
    o hash, entao um dump vazado nao contem sessao utilizavel.
    """
    token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    """SHA-256 em hexadecimal (64 caracteres).

    SHA-256 aqui, argon2 na senha: a diferenca e a entropia da entrada. Argon2 e
    lento de proposito para compensar senha humana fraca. Um token de 384 bits
    aleatorios nao precisa dessa compensacao -- e pagar 200ms a cada refresh seria
    custo sem ganho.
    """
    return hashlib.sha256(token.encode()).hexdigest()
