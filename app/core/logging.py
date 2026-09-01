"""Log estruturado em JSON, com ID de correlacao.

## Por que JSON e nao texto

Log em texto e otimo para uma pessoa lendo um terminal e inutil para tudo o
mais. "Nao consigo entrar desde ontem a tarde" vira uma busca por substring
num arquivo, e a resposta depende de alguem lembrar o formato exato da linha.

Em JSON, cada campo e consultavel: filtrar por `request_id`, por rota, por
status, por usuario. Qualquer agregador (Loki, CloudWatch, Datadog) le isso
sem parser proprio.

## Por que o ID de correlacao

Um pedido do usuario vira varias linhas de log, em varios modulos, e num
servidor com concorrencia elas chegam intercaladas com as de outros pedidos.
Sem um identificador comum, reconstruir "o que aconteceu naquela requisicao"
e adivinhacao.

O ID vive num `ContextVar`, que em asyncio e por TAREFA: cada requisicao tem o
seu, e uma nao enxerga o da outra. Uma variavel global comum daria o valor da
ultima requisicao que passou -- e o log apontaria para o pedido errado, o que
e pior que nao ter ID nenhum.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# `default=""` e nao `None`: o log tambem roda fora de uma requisicao (startup,
# job de snapshot, migrations), e ali nao ha correlacao para registrar.
id_da_requisicao: ContextVar[str] = ContextVar("id_da_requisicao", default="")

# Campos que o `logging` ja poe em todo registro. Tudo que NAO estiver aqui e
# extra passado pelo chamador (`logger.info(..., extra={...})`) e vai para o
# JSON automaticamente -- e assim adicionar um campo novo nao exige mexer no
# formatador.
_PADRAO = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName""".split()
)


class FormatadorJson(logging.Formatter):
    """Uma linha, um objeto JSON."""

    def format(self, record: logging.LogRecord) -> str:
        dados: dict[str, Any] = {
            # ISO 8601 em UTC: fuso do servidor e detalhe de infraestrutura, e
            # comparar horarios entre maquinas com fusos diferentes e uma fonte
            # classica de investigacao perdida.
            "hora": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensagem": record.getMessage(),
        }

        correlacao = id_da_requisicao.get()
        if correlacao:
            dados["request_id"] = correlacao

        if record.exc_info:
            # O traceback vai num campo, nao concatenado na mensagem: assim o
            # agregador consegue agrupar erros iguais e contar ocorrencias.
            dados["excecao"] = self.formatException(record.exc_info)

        for chave, valor in record.__dict__.items():
            if chave not in _PADRAO and not chave.startswith("_"):
                dados[chave] = valor

        # `default=str` para o log nunca ser a causa da falha: um objeto que o
        # json nao saiba serializar viraria excecao DENTRO do tratamento de
        # erro, escondendo o problema original.
        return json.dumps(dados, ensure_ascii=False, default=str)


def configurar(nivel: str = "INFO", *, json_ativo: bool = True) -> None:
    """Instala o formatador na raiz.

    `json_ativo=False` volta ao texto legivel -- em desenvolvimento, ler JSON
    cru no terminal atrapalha mais do que ajuda.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        FormatadorJson()
        if json_ativo
        else logging.Formatter("%(levelname)-8s %(name)s: %(message)s")
    )

    raiz = logging.getLogger()
    # Substitui os handlers em vez de somar: chamar isto duas vezes (a factory
    # da app roda uma vez por instancia, e a suite cria varias) duplicaria cada
    # linha de log.
    raiz.handlers = [handler]
    raiz.setLevel(nivel)

    # O uvicorn instala os proprios handlers e produziria a linha de acesso em
    # texto ao lado do nosso JSON. Deixamos os registros propagarem para a raiz.
    for nome in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(nome)
        log.handlers = []
        log.propagate = True
