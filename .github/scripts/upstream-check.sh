#!/usr/bin/env bash
# Compare upstream main with the commit this fork's `patched` branch is based on.
# When upstream is ahead, open one issue (or comment on the open one).
#
# Env:
#   GH_TOKEN             token for gh (the workflow passes GITHUB_TOKEN)
#   GITHUB_REPOSITORY    owner/name of this fork (set by Actions)
#   UPSTREAM_BASE_FILE   file holding the base SHA (default UPSTREAM_BASE)
#   DRY_RUN=1            print the issue action instead of doing it
set -euo pipefail

upstream="skipperro/pajgps-homeassistant"
title="Upstream moved: rebase patched"
base_file="${UPSTREAM_BASE_FILE:-UPSTREAM_BASE}"

base="$(tr -d '[:space:]' < "$base_file")"
if ! [[ "$base" =~ ^[0-9a-f]{40}$ ]]; then
  echo "UPSTREAM_BASE must hold a full 40 character SHA, got: '$base'" >&2
  exit 1
fi

ahead="$(gh api "repos/${upstream}/compare/${base}...main" --jq .ahead_by)"
if ! [[ "$ahead" =~ ^[0-9]+$ ]]; then
  echo "Unexpected ahead_by value: '$ahead'" >&2
  exit 1
fi
echo "upstream main is ${ahead} commit(s) ahead of ${base}"

if [ "$ahead" -eq 0 ]; then
  exit 0
fi

body="Upstream ${upstream} main is ${ahead} commit(s) ahead of ${base}, the commit \`patched\` is based on. Rebase \`patched\` onto upstream main, then update \`UPSTREAM_BASE\`. Nothing is ever pushed back to upstream."

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY RUN: would open or update issue '${title}': ${body}"
  exit 0
fi

existing="$(gh issue list --repo "$GITHUB_REPOSITORY" --state open --limit 100 \
  --json number,title --jq "[.[] | select(.title == \"${title}\")][0].number // empty")"

if [ -n "$existing" ]; then
  gh issue comment "$existing" --repo "$GITHUB_REPOSITORY" --body "$body"
else
  gh issue create --repo "$GITHUB_REPOSITORY" --title "$title" --body "$body"
fi
