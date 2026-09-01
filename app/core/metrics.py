"""Metricas no formato Prometheus.

## O que log e metrica respondem

Nao e a mesma pergunta, e por isso os dois existem.

O log responde "o que aconteceu NAQUELA requisicao" -- e caro de agregar: para
saber a latencia media da ultima hora seria preciso ler e somar milhares de
linhas.

A metrica responde "como o sistema esta se comportando" -- e barata de
agregar e nao guarda o caso individual. Um histograma de latencia ocupa alguns
numeros e responde percentil 95 na hora.

## Cardinalidade e o que quebra um Prometheus

Cada combinacao distinta de rotulos vira uma serie temporal guardada em
memoria. Usar a URL como rotulo (`/transactions/9f2c...`) criaria uma serie
por transacao -- milhares de series de um ponto cada, e o processo cresce ate
morrer.

Por isso o rotulo e a ROTA (`/transactions/{transacao_id}`): o numero de
series fica limitado ao numero de endpoints, que e 30.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

_requisicoes = Counter(
    "http_requisicoes_total",
    "Requisicoes HTTP atendidas",
    ["metodo", "rota", "status"],
)

_duracao = Histogram(
    "http_duracao_segundos",
    "Tempo de resposta, do primeiro middleware ate o ultimo byte",
    ["metodo", "rota"],
    # Faixas escolhidas para ESTA aplicacao, nao as do padrao. O default do
    # cliente vai ate 10s, o que junta tudo o que interessa numa faixa so:
    # aqui a maioria das rotas responde em milissegundos, e as que falam com
    # fornecedor externo levam segundos. As duas pontas precisam de resolucao.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def registrar(metodo: str, rota: str, status: int, duracao: float) -> None:
    """Conta a requisicao e guarda o tempo dela."""
    _requisicoes.labels(metodo=metodo, rota=rota, status=str(status)).inc()
    _duracao.labels(metodo=metodo, rota=rota).observe(duracao)


def exportar() -> tuple[bytes, str]:
    """O corpo e o content-type que o Prometheus espera na coleta."""
    return generate_latest(), CONTENT_TYPE_LATEST
