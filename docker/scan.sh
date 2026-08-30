#!/usr/bin/env bash
# Обвязка образа: выгрузка знаний агентом и анализ кода утилитой tldr за один прогон.
#.
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
# Файл конфигурации кладётся во временный каталог: контейнер запускают под произвольным
# идентификатором пользователя, и домашний каталог образа может быть недоступен на запись.
export GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-${TMPDIR:-/tmp}/gitconfig-scan}"
if ! git config --global --add safe.directory '*' 2>/dev/null; then
  echo "[scan] warning: настройка git не записана, история может быть недоступна" >&2
fi

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
    # Документы анализа складываются в промежуточный каталог: имя каталога прогона
    # вычисляет агент, он же переносит исходники и диагностику к выгрузке.
    STAGE="$OUT/.code-analysis"
    mkdir -p "$STAGE"
    CODE_STATUS=()
    # complexity в перечень не входит: она считает одну функцию в одном файле,
    # а сложность по каталогу возвращает health в разделе complexity.
    for command in structure calls health; do
      file="$STAGE/tldr-$command.json"
      extra=()
      # Без снятия ограничения граф вызовов усекается до двухсот рёбер и помечается truncated.
      if [ "$command" = "calls" ]; then
        extra=(--max-items "${SCAN_MAX_ITEMS:-100000}")
      fi
      echo "[scan]   tldr $command${extra[*]+ ${extra[*]}}"
      if tldr "$command" "$target" "${extra[@]+"${extra[@]}"}" --format json > "$file" 2> "$STAGE/tldr-$command.err"; then
        PRODUCED+=("tldr-$command.json")
        CODE_STATUS+=("\"$command\": true")
        rm -f "$STAGE/tldr-$command.err"
      else
        # Диагностика остаётся в промежуточном каталоге и переносится к выгрузке вместе
        # с документами: несписанных файлов в каталоге результатов не остаётся.
        echo "[scan]   warning: tldr $command завершился с ошибкой, см. код-анализ прогона" >&2
        PRODUCED+=("tldr-$command.err")
        CODE_STATUS+=("\"$command\": false")
        rm -f "$file"
      fi
    done
  fi
fi

# Сведения, известные обвязке: версии внешних утилит, координата образа и статусы
# команд анализа. Состав результатов описывает агент — он их и создаёт.
METADATA="$OUT/.run-metadata.json"
{
  printf '{\n'
  printf '  "tldr_version": "%s",\n' "$TLDR_VERSION"
  printf '  "git_version": "%s",\n' "$GIT_VERSION"
  printf '  "image": "%s",\n' "${SCAN_IMAGE:-неизвестен}"
  printf '  "image_tag": "%s",\n' "${SCAN_IMAGE_TAG:-неизвестен}"
  # Тег переставляется на новую сборку, поэтому прогон отдельно фиксирует неизменяемые
  # идентификаторы: ревизию, зашитую в образ при сборке, и digest, известный только
  # снаружи контейнера и потому приходящий переменной от вызывающей стороны.
  printf '  "image_revision": "%s",\n' "${SCAN_IMAGE_REVISION:-неизвестна}"
  printf '  "image_digest": "%s",\n' "${SCAN_IMAGE_DIGEST:-неизвестен}"
  printf '  "code_path": "%s",\n' "${CODE_PATH:-}"
  printf '  "agent_arguments": "%s",\n' "${AGENT_ARGS[*]+${AGENT_ARGS[*]}}"
  printf '  "code_analysis": {'
  first=1
  for entry in ${CODE_STATUS[@]+"${CODE_STATUS[@]}"}; do
    [ "$first" -eq 1 ] || printf ', '
    printf '%s' "$entry"
    first=0
  done
  printf '}\n}\n'
} > "$METADATA"

if [ "$EXPORT" -eq 1 ]; then
  echo "[scan] выгрузка знаний о проекте"
  python -m agent.cli export "$PROJECT" --versioned --output "$OUT" \
    --run-metadata "$METADATA" \
    ${STAGE:+--code-analysis "$STAGE"} "${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"}"
  rm -rf "${STAGE:-}"
fi
rm -f "$METADATA"

echo "[scan] готово"
for name in ${PRODUCED[@]+"${PRODUCED[@]}"}; do echo "[scan]   $name"; done
