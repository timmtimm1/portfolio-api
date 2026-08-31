"""Autenticacao de processo automatizado (maquina), nao de pessoa.

## Por que nao reusar o JWT dos usuarios

Um job de cron nao tem senha para digitar nem navegador para guardar cookie. Se
ele usasse o fluxo humano, precisaria de uma conta de usuario com senha
armazenada no CI -- e essa conta teria acesso a TUDO que um usuario tem, quando
so precisa disparar um calculo.

A chave de servico e um credencial separado, com um unico poder: acionar a rota
de snapshots. Ela nao le carteira de ninguem, nao autentica como usuario nenhum,
e nao serve para mais nada. E o principio do menor privilegio aplicado a
identidade de maquina.

## Por que ela e longa e aleatoria, e nao "uma senha forte"

Senha humana e curta porque alguem precisa digitar; por isso exige argon2 para
compensar a entropia baixa. Uma chave de servico vive num cofre de CI e e colada
por um script -- entao pode ter 384 bits de entropia, e a comparacao direta e
segura sem funcao de derivacao cara.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings

CABECALHO = "X-Service-Key"


async def exigir_chave_de_servico(
    settings: Annotated[Settings, Depends(get_settings)],
    x_service_key: Annotated[str | None, Header(alias=CABECALHO)] = None,
) -> None:
    """Autoriza o chamador ou responde 401.

    `secrets.compare_digest` compara em tempo constante. Uma comparacao normal
    (`a == b`) para no primeiro byte diferente, e essa diferenca de tempo e
    mensuravel pela rede: o atacante descobre a chave um caractere por vez, em
    algumas centenas de tentativas em vez de 2^384. E o mesmo raciocinio do login
    da Etapa 2, aplicado a uma comparacao de segredo em vez de a um hash.

    Sem chave configurada, a rota nao existe (404) em vez de ficar aberta.
    Fail-closed: o modo de falha de um deploy incompleto tem que ser "nao
    funciona", nunca "funciona sem protecao".
    """
    if settings.SERVICE_API_KEY is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    esperada = settings.SERVICE_API_KEY.get_secret_value()
    if x_service_key is None or not secrets.compare_digest(x_service_key, esperada):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Chave de servico invalida",
        )


ChaveDeServico = Depends(exigir_chave_de_servico)
