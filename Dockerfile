# Imagem de producao. O compose local nao usa isto -- ele roda a API direto no
# host com `uvicorn --reload` (ver docker-compose.yml). Este Dockerfile existe
# so para o deploy (Render ou qualquer host que aceite uma imagem Docker).

# ── Etapa 1: instala as dependencias com uv ─────────────────────────────────
# Separada da copia do codigo de proposito: o `uv sync` so re-executa quando
# pyproject.toml ou uv.lock mudam. Uma alteracao em app/ reaproveita esta
# camada inteira e o build fica em segundos, nao minutos.
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv==0.8.17

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: usa exatamente as versoes do lock, nunca resolve de novo -- o que
# passou no CI e o que sobe. --no-dev: pytest, mypy e testcontainers nao tem
# lugar na imagem que serve trafego real.
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

# ── Etapa 2: imagem final, so o necessario para rodar ───────────────────────
FROM python:3.12-slim AS runtime

# Usuario nao-root: uma RCE no app so ganha os privilegios deste usuario, nao
# root do container.
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv ./.venv
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini

USER app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Sem --reload: reload observa o filesystem para recarregar o processo, o que
# so faz sentido em desenvolvimento. Em producao e so overhead.
#
# `alembic upgrade head` roda ANTES do uvicorn, dentro do proprio CMD -- e nao
# como "Pre-Deploy Command" do Render, porque esse recurso e pago e o plano
# Free rejeita o blueprint que o usa. Rodar aqui e o padrao de contorno: a
# migration aplica a cada boot do container, o que e seguro (idempotente --
# vira no-op quando ja esta em head) porque o Free tier so mantem UMA
# instancia por vez, sem duas rodando em paralelo por cima do mesmo banco.
#
# `exec` antes do uvicorn e o que faz o processo TROCAR de lugar do shell, em
# vez de rodar como filho dele -- sem isso um SIGTERM do Render (ao reiniciar
# ou desligar o servico) chegaria ao `sh`, nao ao uvicorn, e o desligamento
# deixaria de ser gracioso.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
