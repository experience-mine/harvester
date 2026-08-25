# Цели сборки и запуска слепка проекта. Значения берутся из .env, см. .env.example.

COMPOSE ?= docker compose
SCAN_ARGS ?=

.PHONY: help pull build scan scan-code shell clean

help: ## Показать перечень целей
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

pull: ## Получить готовый образ из реестра
	$(COMPOSE) pull

build: ## Собрать образ локально вместо готового
	$(COMPOSE) build

scan: ## Выгрузить знания о проекте: make scan SCAN_ARGS="-- --exclude 'vendor/'"
	$(COMPOSE) run --rm scanner $(SCAN_ARGS)

scan-code: ## Выгрузка знаний и анализ кода tldr
	$(COMPOSE) run --rm scanner --code $(SCAN_ARGS)

shell: ## Открыть оболочку в образе
	$(COMPOSE) run --rm --entrypoint bash scanner

clean: ## Удалить результаты прогонов
	rm -rf out
