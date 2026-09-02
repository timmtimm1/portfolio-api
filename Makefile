# Atalhos. `make ajuda` lista tudo.
.PHONY: ajuda banco migrar migration api testes cobertura lint tipos verificar

ajuda:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

banco: ## Sobe o Postgres de desenvolvimento
	docker compose up -d

migrar: ## Aplica as migrations pendentes
	uv run alembic upgrade head

# O autogenerate do Alembic emite linhas de 140 caracteres e imports fora de
# ordem -- ou seja, codigo que o proprio lint deste repo recusa. Gerar e
# formatar em passos separados significa lembrar do segundo toda vez, e foi
# exatamente esquecer dele que derrubou o CI duas vezes em 02/09/2026 (o
# codigo estava certo; so o arquivo gerado e que nao passava no ruff).
#
# Uso: make migration m="descricao curta da mudanca"
migration: ## Gera migration e ja formata o arquivo (make migration m="...")
	@test -n "$(m)" || (echo 'Informe a mensagem: make migration m="o que mudou"'; exit 1)
	uv run alembic revision --autogenerate -m "$(m)"
	uv run ruff check --fix migrations -q || true
	uv run ruff format migrations
	@echo
	@echo ">>> Revise o arquivo gerado ANTES de aplicar:"
	@echo "    - CHECK constraints NAO sao detectados (ver include_object em migrations/env.py)"
	@echo "    - coluna NOT NULL em tabela com dados precisa de server_default"

api: ## Sobe a API com reload
	uv run uvicorn app.main:app --reload

testes: ## Roda a suite
	uv run pytest

cobertura: ## Roda a suite com relatorio de cobertura
	uv run pytest --cov --cov-report=term-missing

lint: ## ruff (checagem e formatacao)
	uv run ruff check --fix . && uv run ruff format .

tipos: ## mypy em modo strict
	uv run mypy app

verificar: lint tipos testes ## Tudo que o CI roda -- use antes de commitar
