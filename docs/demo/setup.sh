# Sourced (off screen) by docs/demo/demo.tape: a throwaway home with offline fixtures, so the
# demo never touches your own ~/.jobhunt or the network. Everything shown is synthetic.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PERSONA=intl-sponsorship
DEMO=/tmp/jobhunt-demo
rm -rf "$DEMO" && mkdir -p "$DEMO"
export HOME="$DEMO" JOBHUNT_HOME="$DEMO/.jobhunt" JOBHUNT_FIXTURES="$DEMO/fixtures"
export PATH="$REPO/.venv/bin:$PATH" PS1='$ ' COLUMNS=100
cp -R "$REPO/tests/fixtures/sources/base" "$JOBHUNT_FIXTURES"
cp -R "$REPO/tests/fixtures/forms/overlay/." "$JOBHUNT_FIXTURES/"
cd "$DEMO" || return

# Onboarding files for the synthetic persona (after `doctor --fix` creates the data folder).
persona() {
  local src="$REPO/tests/personas/$PERSONA"
  cp "$src"/{profile,preferences,answers,watchlist}.yaml "$JOBHUNT_HOME/"
  cp "$src/resume.md" "$JOBHUNT_HOME/resume/"
  # Judge every job, not only the top priorities, so every band in the demo is final.
  printf 'fit:\n  priority_threshold: 0\n' > "$JOBHUNT_HOME/config.yaml"
}

# Stands in for the AI doing the judgment tasks (the jobhunt-judge skill): the deterministic
# helper the test suite uses writes results that `jobhunt ingest` then checks as usual.
judge() {
  python -c "import sys; sys.path.insert(0, '$REPO'); from pathlib import Path; \
from tests.judge_helpers import judge_all; judge_all(Path('$JOBHUNT_HOME'))"
}
