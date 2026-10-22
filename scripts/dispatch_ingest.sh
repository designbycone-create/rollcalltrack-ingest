#!/usr/bin/env bash
# Choose the Disclosure Ingest command for a schedule or workflow_dispatch.
#
# GitHub coerces choice options "true" and "false" to booleans. The selected
# "true" then fails to match and the default false is stored instead. Run
# 39839118977 selected dry_run=true and the step was rewritten to DRY="false"
# before bash started, so the old "anything but true posts" branch ran --live
# and wrote all 659 session-1 rolls.
#
# Only the exact token "live" posts on a manual run. true, false, empty, and
# any other value parse and do not POST. The schedule ignores this input.
#
# Env:
#   DISPATCH_EVENT     github.event_name (schedule | workflow_dispatch)
#   DISPATCH_SESSION   github.event.inputs.senate_session (raw string)
#   DISPATCH_DRY_RUN   github.event.inputs.dry_run (raw string, not inputs.*)
#   DISPATCH_PRINT_ONLY  when 1, print the decision and do not exec
set -euo pipefail

event="${DISPATCH_EVENT:-}"
session="${DISPATCH_SESSION:-}"
mode="${DISPATCH_DRY_RUN:-}"

# Schedule is the daily job: current session, and it does post.
# A manual run posts only when the operator picked the non-boolean token.
if [ "$event" = "schedule" ]; then
  live=1
  session="current"
elif [ "$mode" = "live" ]; then
  live=1
else
  live=0
fi

if [ -z "$session" ]; then
  session="current"
fi

case "$session" in
  current|1|2|all) ;;
  *)
    echo "Invalid senate_session: $session" >&2
    exit 1
    ;;
esac

if [ "$session" = "current" ]; then
  cmd=(python -m ingest.main --source all)
else
  cmd=(python -m ingest.main --source senate_votes --senate-session "$session")
fi

if [ "$live" -eq 1 ]; then
  export INGEST_LIVE=1
  cmd+=(--live)
else
  # Drop a step-level INGEST_LIVE so a missed --dry-run still cannot write.
  unset INGEST_LIVE || true
  cmd+=(--dry-run)
fi

echo "dispatch event=${event} senate_session=${session} dry_run=${mode} live=${live}"
if [ "${DISPATCH_PRINT_ONLY:-}" = "1" ]; then
  printf 'ingest_live=%s\n' "${INGEST_LIVE:-}"
  printf 'argv=%s\n' "${cmd[*]}"
  exit 0
fi

echo "Running: ${cmd[*]}"
exec "${cmd[@]}"
