#!/usr/bin/env bash
# The golden corpus every night on the owner's machine (#87): renders origin/main, compares it with the accepted
# baseline (the first run becomes it; `python -m seedbox2.eval golden accept RUN` moves it) and posts the summary to
# the board. A crontab line, or the same from a systemd user timer:
#
#   30 2 * * * SEEDBOX2_GOLDEN_AUDIO=/mnt/media/MUSIC:$HOME/golden-free /path/to/Seedbox/seedbox2/eval/golden-nightly.sh
#
# Environment:
#   SEEDBOX2_GOLDEN_AUDIO    folders with the corpus's audio (see `python -m seedbox2.eval golden check`)
#   SEEDBOX2_GOLDEN_PYTHON   the Python to use (default: .venv/bin/python of this checkout)
#   SEEDBOX2_GOLDEN_UPDATE   0: run this checkout as it is instead of origin/main
#   KANBAN                   the board's server (default: http://localhost:4317)
#   SEEDBOX2_GOLDEN_BOARD    the board (default: Seedbox's)
#   SEEDBOX2_GOLDEN_TICKET   the ticket that collects the summaries. By default the script files one and remembers it:
#                            unassigned and tagged `blocked`, so no agent claims it, as comments on a ticket an agent
#                            holds would wake that agent. (The Done column only takes tickets with a merged pull request.)
#
# Exit status: 0 fine, 1 regressions, 2 no run or the summary couldn't be posted.

set -uo pipefail

repo=$(git -C "$(dirname "$(readlink -f "$0")")" rev-parse --show-toplevel) || exit 2
python=${SEEDBOX2_GOLDEN_PYTHON:-$repo/.venv/bin/python}
kanban=${KANBAN:-http://localhost:4317}
board=${SEEDBOX2_GOLDEN_BOARD:-w118DWMEAfM}
actor=golden-nightly

results=$(cd "$repo" && "$python" -c 'from seedbox2.eval.run import results_dir; print(results_dir())') || exit 2
state=$results/golden
mkdir -p "$state"
log=$state/nightly.log
summary=$state/nightly-summary.md
: >"$log"

work=$repo
if [[ ${SEEDBOX2_GOLDEN_UPDATE:-1} != 0 ]]; then  # the owner's checkout may be on any branch: test main in a worktree
  work=$state/worktree
  git -C "$repo" fetch -q origin main >>"$log" 2>&1
  if [[ -e $work/.git ]]; then
    git -C "$work" checkout -q --detach origin/main >>"$log" 2>&1
  else
    git -C "$repo" worktree prune >>"$log" 2>&1
    git -C "$repo" worktree add -q --detach "$work" origin/main >>"$log" 2>&1
  fi || work=
fi

status=2
rm -f "$summary"
if [[ -n $work ]]; then
  (cd "$work" && "$python" -m seedbox2.eval golden --baseline accepted --summary "$summary" --keep-runs 14) >>"$log" 2>&1
  status=$?
fi
if [[ ! -s $summary ]]; then
  { echo "**Golden corpus**: the nightly run failed (exit status $status). The end of \`$log\`:"; echo; echo '```'
    tail -n 15 "$log"; echo '```'; } >"$summary"
fi

api() {  # api PATH JSON: POST to the board, print the response
  curl -sS -f -m 30 -X POST "$kanban/api$1" -H 'Content-Type: application/json' -H "X-Actor: $actor" -d "$2"
}
json() { "$python" -c 'import json, sys; print(json.dumps({sys.argv[1]: sys.stdin.read()}))' "$1"; }

ticket=${SEEDBOX2_GOLDEN_TICKET:-$(cat "$state/board-ticket" 2>/dev/null)}
if [[ -z $ticket ]]; then
  ticket=$(api "/boards/$board/tickets" '{"title":"Seedbox: golden corpus nightly results","tags":["blocked"],"description":"Each night seedbox2/eval/golden-nightly.sh posts its summary here (#87)."}' \
    | "$python" -c 'import json, sys; print(json.load(sys.stdin)["id"])') || { echo "couldn't file the results ticket on $kanban" >&2; exit 2; }
  echo "$ticket" >"$state/board-ticket"
fi
if ! json body <"$summary" | { read -r body; api "/tickets/$ticket/comments" "$body" >/dev/null; }; then
  echo "couldn't post the summary to ticket $ticket on $kanban; it is in $summary" >&2
  exit 2
fi
exit "$status"
