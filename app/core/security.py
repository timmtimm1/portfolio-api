"""Hash de senha e emissao/validacao de JWT.

Este modulo nao conhece banco, nem FastAPI, nem models. E proposital: funcoes
puras sao as unicas que da para testar exaustivamente, e criptografia e
exatamente o codigo que voce quer testar exaustivamente.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
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


# --- Versoes assincronas: as UNICAS que codigo `async` pode chamar ------------
#
# O argon2 daqui custa ~80 ms de CPU e 64 MiB por hash, e com parallelism=4 um
# hash sozinho ja ocupa quatro nucleos. Chamado direto dentro de `async def`, ele
# congela o event loop pelo tempo inteiro do calculo -- e com o loop parado
# nenhuma outra requisicao anda, nem o health check. Medido em 11/09/2026 numa
# maquina de 4 nucleos, mediana de 3 rodadas nas mesmas condicoes: 8 logins
# simultaneos pararam o loop por 1.138 ms chamados direto, e por 22 ms neste
# pool (tempo total de 1.138 para 831 ms). Com 4: 415 ms contra 24 ms. Os
# valores absolutos variam muito entre rodadas; a proporcao, nao.
#
# Por que um pool dedicado, e nao `asyncio.to_thread`:
#   - `to_thread` usa o executor padrao (8 threads nesta maquina). Uma rajada de
#     logins roda 8 hashes ao mesmo tempo sem ganhar velocidade -- um hash ja
#     satura os nucleos -- e aloca 64 MiB x 8. O rate limit e por IP; com
#     alguns IPs, 50 logins simultaneos sao 3 GB. Negacao de servico pela
#     memoria.
#   - Um pool de N trabalhadores E o teto: o hash excedente espera na fila do
#     pool sem segurar thread nem memoria.
#   - Nao um `asyncio.Semaphore` global: ele se prende ao primeiro event loop que
#     o usa, e a suite de testes abre um loop por teste.
_TRABALHADORES_ARGON2 = max(1, (os.cpu_count() or 2) // 2)
_POOL_ARGON2 = ThreadPoolExecutor(max_workers=_TRABALHADORES_ARGON2, thread_name_prefix="argon2")


async def hash_password_async(senha: str) -> str:
    """`hash_password` fora do event loop, no pool dedicado."""
    return await asyncio.get_running_loop().run_in_executor(_POOL_ARGON2, hash_password, senha)


async def verify_password_async(senha: str, hash_armazenado: str) -> tuple[bool, str | None]:
    """`verify_password` fora do event loop, no pool dedicado."""
    return await asyncio.get_running_loop().run_in_executor(
        _POOL_ARGON2, verify_password, senha, hash_armazenado
    )


async def verify_password_dummy_async(senha: str) -> None:
    """`verify_password_dummy` fora do event loop -- pela MESMA fila do verify real.

    Isso e parte da defesa de timing, nao detalhe de implementacao. Se o hash
    descartavel furasse a fila (chamado direto, ou num pool proprio mais
    folgado), "email inexistente" voltaria a responder mais rapido que "senha
    errada" justamente sob carga -- que e quando alguem esta enumerando contas.
    """
    await asyncio.get_running_loop().run_in_executor(_POOL_ARGON2, verify_password_dummy, senha)


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


# --- Token de confirmacao de e-mail ------------------------------------------


def generate_confirmation_token() -> tuple[str, str]:
    """(token em texto puro, hash para o banco) do link de confirmacao.

    MESMA construcao do refresh token -- 384 bits de `secrets`, SHA-256 no banco
    --, reaproveitada em vez de reescrita: criptografia duplicada e onde uma das
    copias fica para tras quando a outra e corrigida.
    """
    return generate_refresh_token()


def hash_confirmation_token(token: str) -> str:
    """Hash usado para achar o link no banco. Ver `generate_confirmation_token`."""
    return hash_refresh_token(token)
