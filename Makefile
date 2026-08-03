VENV := .venv
BIN := $(VENV)/bin

.PHONY: install format lint test image chart clean

install:
	python3 -m venv $(VENV)
	$(BIN)/pip install -e '.[dev]'

format:
	$(BIN)/black .
	$(BIN)/isort .
	shfmt -i 2 -w docker/entrypoint.sh

lint:
	$(BIN)/black --check .
	$(BIN)/isort --check-only .
	$(BIN)/flake8 src tests
	$(BIN)/pylint src tests
	shellcheck docker/entrypoint.sh
	shfmt -i 2 -d docker/entrypoint.sh

test:
	$(BIN)/pytest -v

image:
	docker build -f docker/Dockerfile -t hermit .

chart:
	helm lint helm/hermit
	helm template hermit helm/hermit > /dev/null

clean:
	rm -rf $(VENV) .pytest_cache
