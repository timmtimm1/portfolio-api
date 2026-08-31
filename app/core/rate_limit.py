"""Limite de requisicoes por IP.

Por que isso e uma medida de seguranca, nao so de capacidade:

  - Login sem limite e um ataque de forca bruta com convite. Argon2 torna cada
    tentativa cara (~200ms), o que ja ajuda -- mas 200ms x milhares de tentativas
    ainda quebra senha fraca, e ainda derruba o servidor de tabela.
  - Cadastro sem limite permite varrer uma lista de emails atras dos 409 e
    descobrir quem tem conta. E a mitigacao que prometemos ao aceitar devolver
    409 em vez de esconder a existencia da conta.
  - Refresh sem limite permite tentar adivinhar refresh token no atacado.

Limitacao conhecida e deliberada: o armazenamento e em memoria do processo. Com
varios workers, cada um conta o seu -- o limite efetivo e N vezes maior. Para
producao de verdade troca-se `RATE_LIMIT_STORAGE` por uma URI de Redis
(`redis://...`) sem mudar nenhuma outra linha. Fica em memoria porque o free tier
do deploy roda um worker so, e um Redis a mais seria complexidade sem beneficio.

O que NAO fazemos, tambem de proposito: bloquear a conta apos N erros. Bloqueio
por conta e ele proprio um vetor de ataque -- basta errar a senha de alguem de
proposito para trancar essa pessoa do lado de fora. Limitar por IP nao tem esse
efeito colateral.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings


def _chave(request: object) -> str:
    """Identifica quem esta chamando.

    `get_remote_address` le o IP da conexao. Atras de um proxy (Render, Fly) o IP
    real vem em X-Forwarded-For -- mas confiar nesse cabecalho sem o proxy estar
    configurado para reescreve-lo e pior que nao ter limite: qualquer um forja o
    cabecalho e ganha um balde novo a cada request. Por isso usamos o IP da
    conexao e configuramos o proxy no deploy (Etapa 12), nao o contrario.
    """
    return get_remote_address(request)  # type: ignore[arg-type]


limiter = Limiter(
    key_func=_chave,
    storage_uri=get_settings().RATE_LIMIT_STORAGE,
    # Desligado no ambiente de teste: uma suite que roda 40 requests de login
    # esbarraria no limite e falharia por motivo errado.
    enabled=get_settings().ENVIRONMENT != "test",
)


def _segundos_da_janela(descricao: str) -> int:
    """Extrai a janela do limite ("5/minute" -> 60) para o cabecalho Retry-After.

    Nao e o tempo exato ate liberar (isso exigiria consultar o balde), e sim o
    teto da janela -- que e um valor seguro: o cliente nunca tenta cedo demais.
    """
    unidades = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    for nome, segundos in unidades.items():
        if nome in descricao:
            return segundos
    return 60


async def excesso_de_requisicoes(request: Request, exc: Exception) -> JSONResponse:
    """Resposta do 429, com `Retry-After`.

    O handler padrao do slowapi nao inclui esse cabecalho, e sem ele o cliente
    so pode chutar quanto esperar -- normalmente tentando cedo demais e
    prolongando o proprio bloqueio. O RFC 6585 define o 429 justamente com esse
    cabecalho; omiti-lo transforma um limite util num limite hostil.
    """
    limite = getattr(exc, "detail", "") or ""
    segundos = _segundos_da_janela(str(limite))
    return JSONResponse(
        status_code=429,
        content={"detail": f"Muitas requisicoes. Tente de novo em {segundos}s."},
        headers={"Retry-After": str(segundos)},
    )
