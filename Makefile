# Atalhos. `make ajuda` lista tudo.
.PHONY: ajuda banco migrar api testes cobertura lint tipos verificar

ajuda:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

banco: ## Sobe o Postgres de desenvolvimento
	docker compose up -d

migrar: ## Aplica as migrations pendentes
	uv run alembic upgrade head

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
