# Deploy: Neon + Render

Guia para tirar a API do "roda só na máquina que subiu o Docker" e colocar num
endereço único, acessível de qualquer PC (ou celular). Depois disso os dois
notebooks — e qualquer outro dispositivo — abrem a mesma URL e veem os mesmos
dados, porque passam a falar com o mesmo banco.

**Sem instalação nova nos PCs.** O painel é servido pela própria API
(`app/main.py`, seção "Frontend servido pela PRÓPRIA API"), então acessar o
deploy é abrir a URL no navegador. O ambiente local (`make banco` / `make api`)
continua existindo — é o que você usa para *desenvolver*, testado antes de subir.

---

## 1. Banco: um projeto novo no Neon

Você já tem conta no Neon (usada no `mercado_financeiro`/`volume-scanner-b3`).
Este projeto ganha um banco **separado** — não faz sentido dividir schema com
outro produto.

1. Entre em [neon.tech](https://neon.tech) com a conta que já usa.
2. **New Project** → nome `portfolio-api` → região mais próxima de você
   (`sa-east-1`/São Paulo, se disponível; senão `us-east-1`).
3. Neon cria o banco `neondb` e te dá uma *connection string* assim:
   ```
   postgresql://usuario:senha@ep-xxxx.sa-east-1.aws.neon.tech/neondb?sslmode=require
   ```
4. Quebre essa URL nas variáveis que o projeto usa (ver `.env.example`):

   | Variável | Vem de |
   |---|---|
   | `POSTGRES_HOST` | `ep-xxxx.sa-east-1.aws.neon.tech` |
   | `POSTGRES_PORT` | `5432` |
   | `POSTGRES_DB` | `neondb` |
   | `POSTGRES_USER` | `usuario` |
   | `POSTGRES_PASSWORD` | `senha` |

   Guarde esses cinco valores — entram no Render no passo 3.

   *(O app monta a URL de conexão sozinho a partir dessas variáveis — ver
   `database_url` em `app/core/config.py`.)*

   O Neon **recusa conexão sem TLS**, então o serviço no Render também precisa
   de `POSTGRES_SSL=true` (está na tabela do passo 3). Localmente ela fica
   `false`: o Postgres do compose não tem TLS nenhum.

---

## 2. Preparar o e-mail: senha de app do Gmail

Em produção a aplicação **se recusa a subir sem SMTP configurado** — é uma
trava proposital em `app/core/config.py` (`_email_em_producao`), porque sem
isso o cadastro pareceria funcionar mas ninguém receberia o link de confirmação.

1. Ative a verificação em duas etapas na sua conta Google, se ainda não tiver.
2. Acesse <https://myaccount.google.com/apppasswords> e gere uma senha de app
   (nome sugerido: "Portfolio Tracker").
3. Guarde a senha de 16 caracteres — entra como `SMTP_PASSWORD` no passo 3.
   **Não é a senha da sua conta Google.**

Se preferir outro provedor (Zoho, Outlook, um transacional como Resend), a
única mudança é `SMTP_HOST`/`SMTP_PORT`/`SMTP_TLS` — o restante do fluxo é igual.

---

## 3. API: novo Web Service no Render

1. Entre em [render.com](https://render.com) e crie conta (GitHub login é o
   mais rápido — ele já lista os repositórios).
2. **New** → **Web Service** → conecte o repositório `timmtimm1/portfolio-api`.
3. Configuração do serviço:
   - **Language/Runtime**: `Docker` (o `Dockerfile` já está no repo)
   - **Branch**: `main`
   - **Instance Type**: `Free`

   Não existe passo de "Pre-Deploy Command" aqui: **o plano Free do Render não
   suporta esse recurso** (é pago; um blueprint que o usa é rejeitado). A
   migration entra de outro jeito — já está dentro do próprio `CMD` do
   `Dockerfile` (`alembic upgrade head && exec uvicorn ...`), rodando a cada
   boot do container, antes da API aceitar requisição. É seguro porque o Free
   tier mantém uma instância só por vez, então não há duas rodando a mesma
   migration ao mesmo tempo. Nada a configurar aqui — já vem pronto.
4. **Environment** → adicione as variáveis abaixo. Os "❓" pedem um valor
   gerado ou escolhido por você:

   | Variável | Valor |
   |---|---|
   | `ENVIRONMENT` | `production` |
   | `POSTGRES_HOST` | do passo 1 |
   | `POSTGRES_PORT` | `5432` |
   | `POSTGRES_DB` | do passo 1 |
   | `POSTGRES_USER` | do passo 1 |
   | `POSTGRES_PASSWORD` | do passo 1 |
   | `POSTGRES_SSL` | `true` — o Neon recusa conexão sem TLS |
   | `SECRET_KEY` | ❓ gere com `python3 -c "import secrets; print(secrets.token_urlsafe(64))"` |
   | `SERVICE_API_KEY` | ❓ gere com `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` — chave do cron de snapshots |
   | `APP_URL` | a URL que o Render atribuir ao serviço, ex. `https://portfolio-api-xxxx.onrender.com` (dá para conferir/copiar depois do primeiro deploy e atualizar aqui) |
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | seu e-mail do Gmail |
   | `SMTP_PASSWORD` | a senha de app do passo 2 |
   | `SMTP_TLS` | `starttls` |
   | `SMTP_REMETENTE` | `Portfolio Tracker <seu-email@gmail.com>` |
   | `LOG_JSON` | `true` |
   | `RISK_FREE_RATE` | `0.10` (ou a Selic/CDI atual) |
   | `BRAPI_TOKEN` | opcional — deixe em branco ou pegue o seu em brapi.dev |

   `CORS_ORIGINS` fica **de fora** de propósito: o painel é servido pela
   própria API (mesma origem), então CORS não entra em jogo — adicionar a
   variável sem necessidade só abriria uma superfície que não existe hoje.

5. **Create Web Service**. O primeiro build demora (baixa a imagem base,
   instala as dependências); os próximos reaproveitam cache.
6. Quando o deploy terminar, copie a URL pública e cole de volta em `APP_URL`
   nas variáveis de ambiente (ela só existe depois do primeiro deploy) —
   depois disso o Render redeploya sozinho.

**Free tier hiberna após 15 min sem tráfego.** A primeira requisição depois de
um tempo parado demora ~1 min para acordar — normal, não é erro.

---

## 4. Ligar o cron de snapshots do GitHub Actions

O workflow `.github/workflows/snapshots.yml` já existe e já sabe chamar a API
em produção — só está inativo por faltar os secrets.

No GitHub: **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**, duas vezes:

| Secret | Valor |
|---|---|
| `API_URL` | a mesma URL pública do Render (sem `/` no final) |
| `SERVICE_API_KEY` | o mesmo valor gerado no passo 3 |

Sem isso o workflow continua rodando (é agendado), só pula o disparo com um
aviso — não quebra nada ficar para depois.

---

## 5. Testar

```bash
curl https://SUA-URL.onrender.com/api/v1/health
# {"status":"ok","environment":"production","version":"0.1.0"}
```

Abra `https://SUA-URL.onrender.com/painel/`, cadastre-se com o e-mail
principal, confira que o e-mail de confirmação chegou de verdade (não mais
`var/emails/*.eml` — isso só existe fora de produção) e entre.

A partir daqui, os dois PCs (e o celular) usam essa mesma URL. O
`docker compose` local continua de pé para quando você for mexer no código —
sem afetar o que já está no ar.

---

## O que ainda fica de fora

- **Domínio próprio**: o Render já dá HTTPS na URL `.onrender.com`; um domínio
  customizado é configuração extra do Render, não deste guia.
- **Atualização de histórico de preços** (`scripts/atualizar_historico.sh`):
  continua rodando via cron **local**, apontado para o Postgres do Neon (basta
  usar as mesmas variáveis `POSTGRES_*` do passo 1 no `.env` da máquina que
  roda o cron). Como o script grava direto no banco, funciona de qualquer PC —
  só precisa rodar em pelo menos um deles com o crontab ativo.
