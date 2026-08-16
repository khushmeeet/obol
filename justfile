# Workspace glue (plan §2). Run from the repository root.

default: check

# Run the ledger test suite (both backends)
test *args:
    cd ledger && uv run pytest {{args}}

# Lint and format-check
lint:
    cd ledger && uv run ruff check src tests
    cd ledger && uv run ruff format --check src tests

# mypy --strict on the pure domain layer
typecheck:
    cd ledger && uv run mypy

# Everything CI runs
check: lint typecheck test

# The M8 corpus gate: import the bean-example fixtures, compare against Beancount
corpus:
    cd ledger && uv run pytest -q -m corpus

# Regenerate the committed corpus fixtures (only after a deliberate Beancount upgrade)
corpus-fixtures:
    ledger/tools/generate_corpus.sh

# Differential fuzzing with a chosen Hypothesis example budget (nightly runs 1500)
fuzz examples="500":
    cd ledger && LEDGER_FUZZ_EXAMPLES={{examples}} uv run pytest -q tests/properties/test_differential.py --hypothesis-show-statistics

# Run every integrity check against a ledger database
validate db:
    uv run --package ledger ledger validate {{db}}

# Timestamped consistent backup of a ledger database (before migrations)
backup db:
    sqlite3 {{db}} "VACUUM INTO '{{db}}.$(date +%Y%m%d-%H%M%S).bak'"
