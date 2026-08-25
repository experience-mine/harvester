#!/usr/bin/env bash
# Обвязка образа: выгрузка знаний агентом и анализ кода утилитой tldr за один прогон.
#
# Использование:
#   scan [опции обвязки] [-- опции agent export]
#
# Опции обвязки:
#   --project ПУТЬ     анализируемый проект в контейнере (по умолчанию /project)
#   --out ПУТЬ         каталог результатов (по умолчанию /out)
#   --code             выполнить анализ кода утилитой tldr
#   --code-path ПУТЬ   что анализировать tldr, относительно проекта (по умолчанию src, иначе корень)
#   --no-export        пропустить выгрузку знаний, оставить только анализ кода
#   --help             показать это сообщение
#
# Всё, что указано после --, передаётся команде agent export без изменений.

set -euo pipefail

PROJECT="${SCAN_PROJECT:-/project}"
OUT="${SCAN_OUT:-/out}"
CODE=0
CODE_PATH=""
EXPORT=1
AGENT_ARGS=()

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project)   PROJECT="$2"; shift 2 ;;
    --out)       OUT="$2"; shift 2 ;;
    --code)      CODE=1; shift ;;
    --code-path) CODE_PATH="$2"; CODE=1; shift 2 ;;
    --no-export) EXPORT=0; shift ;;
    --help|-h)   usage; exit 0 ;;
    --)          shift; AGENT_ARGS+=("$@"); break ;;
    *)           AGENT_ARGS+=("$1"); shift ;;
  esac
done

if [ ! -d "$PROJECT" ]; then
  echo "каталог проекта недоступен: $PROJECT" >&2
  exit 1
fi
mkdir -p "$OUT"

# Рабочая копия принадлежит хозяину хоста, поэтому git отказался бы читать её как чужую.
git config --global --add safe.directory '*' 2>/dev/null || true

# Каталог результатов может лежать внутри анализируемого проекта: без исключения
# файлы предыдущего прогона попали бы в следующую выгрузку.
abs_path() { (cd "$1" 2>/dev/null && pwd) || printf '%s' "$1"; }
PROJECT_ABS="$(abs_path "$PROJECT")"
OUT_ABS="$(abs_path "$OUT")"
case "$OUT_ABS/" in
  "$PROJECT_ABS"/*)
    RELATIVE_OUT="${OUT_ABS#"$PROJECT_ABS"/}"
    if [ -n "$RELATIVE_OUT" ] && [ "$RELATIVE_OUT" != "$OUT_ABS" ]; then
      echo "[scan] каталог результатов внутри проекта, исключаю из обхода: $RELATIVE_OUT/"
      AGENT_ARGS+=(--exclude "$RELATIVE_OUT/")
    fi
    ;;
esac

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
AGENT_VERSION="$(python -c 'import agent; print(agent.AGENT_VERSION)' 2>/dev/null || echo неизвестна)"
TLDR_VERSION="$(tldr --version 2>/dev/null | head -n 1 || echo недоступен)"
GIT_VERSION="$(git --version 2>/dev/null || echo недоступен)"
PRODUCED=()

echo "[scan] проект: $PROJECT | результаты: $OUT"
echo "[scan] агент: $AGENT_VERSION | tldr: $TLDR_VERSION | $GIT_VERSION"

if [ "$EXPORT" -eq 1 ]; then
  echo "[scan] выгрузка знаний о проекте"
  before="$(ls -1 "$OUT" 2>/dev/null || true)"
  python -m agent.cli export "$PROJECT" --versioned --output "$OUT" "${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"}"
  after="$(ls -1 "$OUT" 2>/dev/null || true)"
  while IFS= read -r name; do
    [ -n "$name" ] && PRODUCED+=("$name")
  done < <(comm -13 <(echo "$before" | sort) <(echo "$after" | sort))
fi

if [ "$CODE" -eq 1 ]; then
  if ! command -v tldr > /dev/null 2>&1; then
    echo "[scan] warning: tldr недоступен, анализ кода пропущен" >&2
  else
    target="$PROJECT"
    if [ -n "$CODE_PATH" ]; then
      target="$PROJECT/$CODE_PATH"
    elif [ -d "$PROJECT/src" ]; then
      target="$PROJECT/src"
    fi
    echo "[scan] анализ кода: $target"
    for command in structure calls complexity health; do
      file="$OUT/tldr-$command.json"
      echo "[scan]   tldr $command"
      if tldr "$command" "$target" --format json > "$file" 2> "$OUT/tldr-$command.err"; then
        PRODUCED+=("tldr-$command.json")
        rm -f "$OUT/tldr-$command.err"
      else
        echo "[scan]   warning: tldr $command завершился с ошибкой, см. tldr-$command.err" >&2
        rm -f "$file"
      fi
    done
  fi
fi

# Манифест прогона: чем и когда собраны файлы рядом с ним.
{
  printf '{\n'
  printf '  "started_at": "%s",\n' "$STARTED"
  printf '  "finished_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  "project": "%s",\n' "$PROJECT"
  printf '  "agent_version": "%s",\n' "$AGENT_VERSION"
  printf '  "tldr_version": "%s",\n' "$TLDR_VERSION"
  printf '  "git_version": "%s",\n' "$GIT_VERSION"
  printf '  "agent_arguments": "%s",\n' "${AGENT_ARGS[*]+${AGENT_ARGS[*]}}"
  printf '  "produced": ['
  first=1
  for name in ${PRODUCED[@]+"${PRODUCED[@]}"}; do
    [ "$first" -eq 1 ] || printf ', '
    printf '"%s"' "$name"
    first=0
  done
  printf ']\n}\n'
} > "$OUT/scan.json"

echo "[scan] готово: $OUT/scan.json"
for name in ${PRODUCED[@]+"${PRODUCED[@]}"}; do echo "[scan]   $name"; done
