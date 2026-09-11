"""O cadastro e o login nao podem congelar o servidor.

Argon2 e caro de proposito: ~80 ms de CPU e 64 MiB por hash. Chamado direto
dentro de `async def`, ele trava o event loop inteiro pelo tempo do calculo, e
com o loop travado nenhuma outra requisicao anda. Medido antes da correcao numa
maquina de 4 nucleos: 8 logins simultaneos pararam o servidor por 1,1 s.

Tres camadas de protecao, da mais barata para a mais cara:
  1. uma varredura de AST garante que nenhum modulo do app chama a versao
     sincrona -- o erro mais provavel e um servico novo importar
     `hash_password` por habito;
  2. um espiao confere EM QUE THREAD o hash roda durante cadastro e login
     reais, sem cronometro nenhum;
  3. uma medicao confere que o loop de fato continua respirando.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.schemas.user import UserCreate
from app.services import auth_service
from app.services.exceptions import CredenciaisInvalidasError
from tests.factories import email_unico

SENHA = "carteira-b3-2026-forte"
SINCRONAS = ("hash_password", "verify_password", "verify_password_dummy")
RAIZ_APP = Path(__file__).resolve().parent.parent / "app"


def test_nenhum_modulo_do_app_chama_o_argon2_sincrono() -> None:
    """Fora de `app/core/security.py`, so as versoes `_async` podem aparecer.

    AST e nao busca de texto: um comentario ou docstring citando
    `hash_password(` nao e chamada, e nao deve reprovar nada.
    """
    ofensas: list[str] = []
    for arquivo in sorted(RAIZ_APP.rglob("*.py")):
        if arquivo.name == "security.py" and arquivo.parent.name == "core":
            continue
        for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
            if not isinstance(no, ast.Call):
                continue
            funcao = no.func
            nome = (
                funcao.id
                if isinstance(funcao, ast.Name)
                else funcao.attr
                if isinstance(funcao, ast.Attribute)
                else None
            )
            if nome in SINCRONAS:
                ofensas.append(f"{arquivo.relative_to(RAIZ_APP.parent)}:{no.lineno} {nome}()")

    assert not ofensas, "argon2 sincrono dentro do app:\n" + "\n".join(ofensas)


async def test_cadastro_e_login_calculam_o_hash_no_pool_dedicado(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministico: registra em que thread cada hash rodou, sem medir tempo.

    A primeira assercao -- a lista ter exatamente os tres hashes esperados -- e a
    que pega a regressao mais provavel. Um servico que volte a fazer
    `from app.core.security import hash_password` guarda a funcao ORIGINAL no
    proprio namespace: o espiao nunca e chamado, e a lista vem vazia em vez de
    vir com o nome da thread errada.
    """
    registro: list[tuple[str, str]] = []
    for nome in SINCRONAS:
        original = getattr(security, nome)

        def espiao(
            *args: object, _nome: str = nome, _original: Callable[..., object] = original
        ) -> object:
            registro.append((_nome, threading.current_thread().name))
            return _original(*args)

        monkeypatch.setattr(security, nome, espiao)

    usuario = await auth_service.criar_usuario(db, UserCreate(email=email_unico(), password=SENHA))
    await auth_service.autenticar(db, usuario.email, SENHA)
    with pytest.raises(CredenciaisInvalidasError):
        await auth_service.autenticar(db, email_unico("ninguem"), SENHA)

    assert [nome for nome, _ in registro] == list(SINCRONAS)
    fora_do_pool = [(nome, thread) for nome, thread in registro if not thread.startswith("argon2")]
    assert not fora_do_pool, f"hash rodou fora do pool dedicado: {fora_do_pool}"


async def _maior_parada(tarefa: Callable[[], Awaitable[object]], n: int) -> float:
    """Roda `n` copias em paralelo; devolve o maior intervalo sem o loop respirar."""
    maior = 0.0
    parar = asyncio.Event()

    async def relogio() -> None:
        nonlocal maior
        ultimo = time.perf_counter()
        while not parar.is_set():
            await asyncio.sleep(0.002)
            agora = time.perf_counter()
            maior = max(maior, agora - ultimo)
            ultimo = agora

    tique = asyncio.create_task(relogio())
    await asyncio.sleep(0.01)
    await asyncio.gather(*(tarefa() for _ in range(n)))
    parar.set()
    await tique
    return maior


async def test_o_event_loop_continua_respirando_durante_os_hashes() -> None:
    """Comparacao RELATIVA, contra a versao sincrona medida no mesmo teste.

    Um limite absoluto em milissegundos quebraria num runner de CI mais lento
    sem nada estar errado. Aqui os dois lados rodam na mesma maquina, no mesmo
    instante: o sincrono para o loop pela soma dos hashes; o assincrono, por
    ruido de escalonamento. A folga de 3x e larga de proposito: medido com 4
    hashes, a diferenca real ficou em torno de 17x.
    """
    hash_ = security.hash_password(SENHA)

    async def sincrono() -> None:
        security.verify_password(SENHA, hash_)

    async def assincrono() -> None:
        await security.verify_password_async(SENHA, hash_)

    parada_sincrona = await _maior_parada(sincrono, n=4)
    parada_assincrona = await _maior_parada(assincrono, n=4)

    assert parada_assincrona < parada_sincrona / 3, (
        f"loop parado {parada_assincrona * 1000:.0f} ms com a versao async, "
        f"contra {parada_sincrona * 1000:.0f} ms com a sincrona"
    )
