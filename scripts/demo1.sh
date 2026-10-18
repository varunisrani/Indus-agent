#!/usr/bin/env bash
# Demo 1 by hand, against a real model provider (P06-14). Not run in CI: CI runs the same flow
# against the fake model in tests/e2e/test_demo1.py.
#
# Needs a provider key in the environment (for example OPENROUTER_API_KEY or ANTHROPIC_API_KEY;
# `indus` picks the provider from the keys it finds) and, optionally, INDUS_DEMO_MODEL to choose
# the model. Runs in a fresh home and a fresh folder so nothing of yours is touched.
#
#   scripts/demo1.sh
#
# Then, in the chat: type `create hello.py and run it`, watch the reply stream, answer the
# approval card with 1, check the output, type /quit. The script reopens the chat with `indus -c`:
# the recap shows the earlier turn; type /history to see it in full, then /quit.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
demo_home="$(mktemp -d "${TMPDIR:-/tmp}/indus-demo1-home.XXXXXX")"
demo_work="$(mktemp -d "${TMPDIR:-/tmp}/indus-demo1-work.XXXXXX")"
export INDUS_HOME="$demo_home"
export INDUS_TERMINAL_ID="demo1-$$"

model_args=()
if [ -n "${INDUS_DEMO_MODEL:-}" ]; then
  model_args=(-m "$INDUS_DEMO_MODEL")
fi

indus() {
  (cd "$demo_work" && uv run --project "$repo" indus "$@")
}

echo "Demo 1: home $demo_home, folder $demo_work"
echo "Type: create hello.py and run it   (answer the card with 1, then /quit)"
indus --cli ${model_args[@]+"${model_args[@]}"}

if [ ! -f "$demo_work/hello.py" ]; then
  echo "demo1: hello.py was not created" >&2
  exit 1
fi

echo
echo "Reopening with indus -c: the recap should show the turn; try /history, then /quit"
indus --cli -c

echo
echo "Demo 1 done. Files: $(ls "$demo_work")"
