# Portfolio Tracker API

API REST para acompanhamento de carteira de investimentos da B3: o usuário registra
suas compras e vendas, e o sistema calcula posição, rentabilidade, risco e sugere
alocações usando a fronteira eficiente de Markowitz.

> **Em construção.** O projeto está sendo desenvolvido em etapas curtas e verificáveis;
> o quadro abaixo mostra onde ele está.

| # | Etapa | Estado |
|---|---|---|
| 0 | Fundação: FastAPI, Docker, configuração *fail-closed* | ✅ |
| 1 | Camada de dados async, Alembic, model `User` | ✅ |
| 2 | Registro e login (argon2id + JWT) | ✅ |
| 3 | Sessão: rotação de refresh token, detecção de reuso, rate limit | ✅ |
| 4 | Suíte de testes com Postgres efêmero + CI | ✅ |
| 5 | Catálogo de ativos da B3 e carga histórica | ⬜ |
| 6 | Livro de transações e cálculo de posição | ⬜ |
| 7 | Cotações (brapi/yfinance) com cache | ⬜ |
| 8 | Métricas: retorno, volatilidade, correlação | ⬜ |
| 9 | Otimização de Markowitz | ⬜ |
| 10 | Snapshots diários da carteira | ⬜ |
| 11 | Frontend com o gráfico da fronteira | ⬜ |
| 12 | Deploy | ⬜ |

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Alembic · PostgreSQL 17 ·
pytest + testcontainers · uv · ruff · mypy strict

## Arquitetura

As dependências apontam sempre para baixo. A camada de serviço não importa FastAPI,
e o módulo de segurança não conhece banco nem models — é o que permite reusar as
regras num job de cron e testar a criptografia de forma exaustiva, sem Postgres.

```
routers/    HTTP: status code, cabeçalho, cookie
   ↓
schemas/    validação e normalização da entrada (≠ o que está no banco)
   ↓
services/   regras de negócio; levantam exceções de domínio, não HTTPException
   ↓
models/     tabelas
   ↓
core/       config, sessão de banco, segurança, dependências
```

## Decisões de segurança

Cada uma está justificada na docstring do módulo correspondente.

- **A aplicação não sobe sem segredo.** `SECRET_KEY` e a senha do banco não têm valor
  padrão; uma chave com menos de 32 caracteres é rejeitada no boot. Variável de
  ambiente com typo vira erro de inicialização (`extra="forbid"`), não um segredo
  silenciosamente ignorado.
- **Segredos não vazam em log.** `SecretStr` mascara em `repr()`, e a URL do banco é
  um objeto `URL` do SQLAlchemy, que oculta a senha em traceback e em mensagem de
  erro de conexão.
- **Senha com argon2id** (`pwdlib`), com regravação automática do hash no login quando
  os parâmetros mudam. Limite de 128 caracteres na entrada: sem teto, uma senha de
  megabytes vira negação de serviço no hash.
- **Login em tempo constante.** Email inexistente paga o custo de um argon2 descartável.
  Sem isso, a resposta em ~2 ms denunciaria quais emails têm conta, mesmo com a
  mensagem de erro idêntica.
- **JWT com `algorithms` fixo** e claim `typ`: barra o ataque `alg=none` e a confusão
  entre token de acesso e de renovação.
- **Refresh token não é JWT.** É um valor opaco de 384 bits, guardado no banco apenas
  como SHA-256 — um dump vazado não contém sessão utilizável. Viaja em cookie
  `httpOnly` + `SameSite=Strict` com path restrito: XSS não lê, CSRF não envia.
- **Rotação com detecção de reuso.** Cada refresh token vale uma vez; reapresentar um
  já rotacionado derruba todas as sessões do usuário (RFC 9700).
- **Rate limit por IP** no login, cadastro e refresh — não bloqueio por conta, que
  seria negação de serviço contra o próprio usuário.
- **Chave primária em UUID**: não há id sequencial para enumerar.
- **CSP, HSTS, `X-Frame-Options`, `nosniff`, `Referrer-Policy`** em toda resposta.
- **`pip-audit` no CI**, semanalmente — a maior parte das falhas de uma aplicação não
  está no código dela, está no que ela importa.

## Rodando localmente

```bash
cp .env.example .env          # gere SECRET_KEY: python -c "import secrets; print(secrets.token_urlsafe(64))"
uv sync
make banco                    # Postgres via Docker
make migrar
make api                      # http://127.0.0.1:8000/docs
```

## Testes

```bash
make testes       # suíte completa
make cobertura    # com relatório de cobertura
make verificar    # exatamente o que o CI roda: lint + tipagem + testes
```

A suíte sobe um **PostgreSQL real e descartável** via testcontainers e aplica as
migrations nele — testar em SQLite e rodar em Postgres seria testar outro banco.
Cada teste roda dentro de uma transação com rollback, então a ordem de execução não
importa.

## Licença

MIT
