#!/usr/bin/env bash
# Regenerate the bean-example corpus fixtures (plan §8.2).
#
# The fixtures are committed rather than generated at test time: an
# upstream change to bean-example must not silently alter the test
# corpus. Rerun this script deliberately — after a pinned Beancount
# upgrade — and commit the diff as its own change.
set -euo pipefail

cd "$(dirname "$0")/.."

for seed in 1 2 3 4 5; do
  uv run bean-example --seed "$seed" \
    --date-begin 2015-01-01 --date-end 2026-01-01 \
    -o "tests/corpus/fixtures/corpus_${seed}.beancount"
  echo "wrote tests/corpus/fixtures/corpus_${seed}.beancount"
done
