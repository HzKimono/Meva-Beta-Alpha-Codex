.PHONY: check

check:
	python -m compileall -q .
	ruff format --check .
	ruff check .
	python -m pytest -q
	python scripts/guard_multiline.py
