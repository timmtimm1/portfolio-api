#!/usr/bin/env bash
# Fecha o elo que faltava na cadeia de atualizacao.
#
# A geracao do CSV ja e automatica: o GitHub Actions do `mercado_financeiro`
# roda de seg a sex as 18h15 (horario de Brasilia) e commita
# data/quotes_history.csv no repo. O que faltava era puxar essa novidade para
# DENTRO do banco deste projeto -- ate aqui, so acontecia quando alguem
# lembrava de rodar `seed_b3` a mao, e foi exatamente isso que deixou o
# historico parado em 26/08 por dias.
#
# Este script faz o proprio `git pull`, sem depender do cron do outro
# projeto ja ter rodado: `--ff-only` e uma operacao segura de repetir -- se o
# outro cron ja atualizou, aqui vira um no-op rapido.
#
# Pensado pra rodar via cron -- ver `crontab.exemplo.txt` nesta pasta.

set -euo pipefail

PROJETO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGEM_DIR="${1:-$HOME/Projects/mercado_financeiro}"
LOG_DIR="$PROJETO_DIR/logs"
LOG_FILE="$LOG_DIR/atualizar_historico.log"

mkdir -p "$LOG_DIR"
exec >> "$LOG_FILE" 2>&1

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cd "$ORIGEM_DIR"
ANTES=$(git rev-parse HEAD)
git pull --ff-only
DEPOIS=$(git rev-parse HEAD)

if [ "$ANTES" = "$DEPOIS" ]; then
    echo "Sem CSV novo -- nada para importar."
    exit 0
fi

echo "CSV atualizado ($ANTES -> $DEPOIS). Importando para o Postgres deste projeto..."
cd "$PROJETO_DIR"
uv run python -m scripts.seed_b3 --origem "$ORIGEM_DIR"

echo "OK."
