# Portfolio Tracker — instruções do projeto

Rastreador de carteira da B3 com análise de risco. Vitrine técnica (LinkedIn /
recrutadores), não um produto comercial. **Não é corretora: não envia ordem,
não vende nada.** Todo "alvo" é lembrete visual.

Python 3.12 · FastAPI · SQLAlchemy 2.0 async (asyncpg) · Alembic · Postgres 17 ·
uv · ruff · mypy strict · pytest + testcontainers · frontend em JS puro
(`app/static/`, sem framework, sem build).

## Comandos

```bash
make verificar   # lint + tipos + testes -- rode ANTES de commitar
make migration m="descrição"   # gera migration E formata (nunca use o alembic cru)
make banco       # sobe o Postgres
make api         # uvicorn --reload
```

O CI roda `ruff check .` no repositório **inteiro**. Conferir só `app tests`
deixa `migrations/` de fora e o CI reprova — já aconteceu duas vezes.

## Invariantes que não se quebram

**Ledger-as-truth.** Não existe coluna de saldo. Posição, preço médio e
resultado são recalculados das transações a cada consulta. A exceção
deliberada é `portfolio_snapshots`: o valor de mercado de ontem não é
reconstruível porque a cotação é sobrescrita.

**Um único portão de autorização.** `get_current_user` e `get_carteira`
(`app/core/deps.py`). Nenhuma rota aceita `user_id`/`portfolio_id` do corpo.
Recurso de outro usuário responde **404, nunca 403** (403 confirma existência).

**Módulos puros.** `optimizer`, `black_litterman`, `position`, `rebalance`,
`simulation`, `split`,
`dividend`, `target`, `trade` não tocam banco, ORM nem HTTP. Entram dados, saem
dados — testáveis com calculadora. Serviços `*_service.py` são a camada que
persiste.

**Decimal para dinheiro, float para estatística.** A conversão acontece numa
fronteira explícita, nunca no meio da conta.

**`lazy="raise"`** nos relationships: N+1 falha alto, não em silêncio.

**Argon2 nunca no event loop.** Hash e verificação de senha custam ~80 ms de
CPU e 64 MiB cada; chamados direto em `async def`, congelam o servidor inteiro
(8 logins simultâneos pararam tudo por 1,1 s; no pool, 22 ms). Código async usa só
`hash_password_async`, `verify_password_async` e `verify_password_dummy_async`,
que rodam num pool dedicado com metade dos núcleos — o pool também é o teto de
memória. `tests/test_login_async.py` varre `app/` por AST e reprova chamada
síncrona.

**Paginação por offset exige ordem TOTAL.** Todo `ORDER BY` de rota paginada
termina na chave primária. `created_at` vem de `now()`, o início da transação:
tudo gravado no mesmo commit empata, e com empate o Postgres devolve os
empatados em ordem diferente entre páginas — linha repetida e linha sumida.

## Frontend (`app/static/`)

- **Nunca `innerHTML`** com dado da API — monte nós de DOM. Há teste que barra.
- **Campo `type="number"` recebe `paraCampoNumerico()`, nunca `num()`.**
  `num(2500)` devolve "2.500" (pt-BR) e o campo lê como 2,5 — o estrago só
  aparece ao salvar. Há teste que barra.
- `[hidden] { display: none !important }` no CSS é obrigatório: sem ele,
  qualquer `display` do autor anula o atributo `hidden`.

## Fronteira eficiente

O `mu` vem de **Black-Litterman**, não da média histórica. A troca aconteceu
porque o otimizador amplifica erro no retorno esperado muito mais que na
covariância, e média histórica por ativo tem erro-padrão enorme — a carteira
"ótima" concentrava no papel que por acaso mais subiu na janela.

- **`pi` e `mu` são retorno EXCEDENTE**; o resto do sistema usa retorno total.
  `retorno_total()` e `para_excesso()` são a fronteira. Errar isso não estoura:
  as contas fecham e o Sharpe sai negativo na carteira de máximo Sharpe.
- **A frente usa a covariância POSTERIOR**, não a amostral. Ela depende das
  opiniões, então opinar muda até a carteira de mínima variância.
- **`delta` fora da faixa 0,5–10 cai para 2,5.** Numa janela de queda o delta
  amostral fica negativo, `pi` inverte de sinal e o equilíbrio passa a dizer
  que ativo arriscado rende menos.
- **Sem `market_cap` no catálogo, o prior deixa de ser o mercado** e vira peso
  igual — `equilibrio.usou_peso_igual` avisa. O `criar_ativo` dos testes nasce
  sem valor de mercado de propósito.
- Opinião que cita ativo fora do cálculo é **descartada com motivo**, nunca
  aplicada pela metade: zerar o coeficiente ausente transforma "PETR4 supera
  VALE3 em 3 pontos" em "PETR4 rende 3%".

## Cadastro e e-mail

- **Login exige e-mail confirmado.** O cadastro manda um link de uso único
  (24 h). `autenticar` recusa com `EmailNaoConfirmadoError` (403) **depois** de
  conferir a senha — antes disso, viraria oráculo de quem tem conta pendente.
- **O token vai no fragmento** (`/painel/#confirmar=…`), nunca na query: o
  fragmento não chega ao servidor nem aos logs. O JS tira o token da barra de
  endereço antes de chamar a API.
- **O e-mail sai por `BackgroundTasks`** — SMTP fora do ar não derruba o
  cadastro —, e o reenvio responde sempre igual (202), exista a conta ou não.
- **Sem `SMTP_HOST`, fora de produção o e-mail vira `.eml` em `var/emails`**, e o
  link não vai para o log. Em produção a aplicação não sobe sem SMTP, com
  `SMTP_TLS=nenhum` ou com `APP_URL` apontando para localhost.
- Nos testes, `criar_usuario` confirma pelo **link real**, lido da
  `CaixaDeEmailFake` do `conftest`; `confirmar=False` cria conta pendente.
- Contas anteriores à coluna `email_confirmado_em` nasceram confirmadas na
  migration `3621b2863c09`.

## Migrations

- `make migration` (o autogenerate cru emite linhas de 140 chars e o lint reprova).
- **CHECK constraints não são detectados** pelo autogenerate — o `include_object`
  em `migrations/env.py` os exclui de propósito (senão ele apaga os que nascem
  dos enums). CHECK novo se escreve à mão na migration.
- Coluna `NOT NULL` em tabela com dados precisa de `server_default`.
- Enum reusado em duas colunas da mesma tabela precisa de `name=` distinto em
  `coluna_enum` — senão os dois CHECK colidem no mesmo nome.
- Sempre teste o round-trip: `upgrade` → `downgrade -1` → `upgrade`.

## Testes

657 testes. A disciplina é **teste de mutação**: depois de escrever um teste,
quebre o código de propósito e confirme que ele falha. Teste que não sabe
falhar não prova nada — já houve quatro testes vacuosos pegos assim.

Para desfazer uma mutação, use **cópia de backup**, nunca `git checkout` num
arquivo com trabalho não commitado (isso já apagou uma feature inteira aqui).

Para conferir log num teste, pendure um handler no logger do módulo, e não use
`caplog`: `configurar()` troca os handlers da raiz quando o app é criado. E o
`migrations/env.py` usa `disable_existing_loggers=False` porque a suíte roda as
migrations no mesmo processo — com o padrão, todo logger do app ficava mudo.

## Ao trabalhar com o app rodando

**Nunca escreva dados de teste na conta real** (`bernardo@exemplo.com`). Use a
conta de demonstração e **confirme com `/auth/me` que `is_demo` é true antes de
escrever** — o app restaura a sessão real sozinha pelo cookie, e uma checagem
de "o app está visível?" não basta. Já aconteceu.

Não digite senhas em formulários; peça para o Bernardo fazer esse passo.

## Estado atual

Feito: auth (JWT + refresh com detecção de reuso), carteiras real/simuladas,
transações, cotações com cache, proventos, desdobramentos, snapshots,
fronteira eficiente (Black-Litterman), rebalanceamento, Monte Carlo, observabilidade
(JSON logs + Prometheus), conta demo de 2h, confirmação de e-mail no cadastro, carteira simulada na lateral, alvos (stop gain/loss + meta de
acumulação), área de trade (trade ótimo).

Pendente: **deploy** (adiado de propósito), editar transação (não existe
PUT/PATCH), recuperar senha, PWA para celular. Imposto de renda foi excluído
deliberadamente — modelar IR exigiria somar vendas do mês, prejuízo acumulado
e tipo de operação; número fiscal quase certo é pior que nenhum.

Dados de mercado vêm do repo irmão `~/Projects/mercado_financeiro` via CSV,
carregados por cron (ver `scripts/atualizar_historico.sh`).
