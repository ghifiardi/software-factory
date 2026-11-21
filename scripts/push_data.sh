#!/usr/bin/env bash
# Commit everything in a factory-data checkout and push it, retrying on races.
#   PUSH_TOKEN=... scripts/push_data.sh <checkout-dir> "<commit message>" [branch]
#
# The token is passed per command as an HTTP header and never written to
# .git/config, so nothing that runs later in the job can read it from disk.
set -euo pipefail

dir="${1:?checkout dir}"
msg="${2:?commit message}"
branch="${3:-factory-data}"
: "${PUSH_TOKEN:?PUSH_TOKEN must be set}"

cd "$dir"
auth="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$PUSH_TOKEN" | base64 | tr -d '\n')"
echo "::add-mask::$(printf 'x-access-token:%s' "$PUSH_TOKEN" | base64 | tr -d '\n')"

git config user.name "factory-bot"
git config user.email "factory-bot@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "nothing to record"
  exit 0
fi
git commit -qm "$msg"

for i in 1 2 3 4 5 6; do
  if git -c http.extraheader="$auth" pull --rebase -q origin "$branch" \
     && git -c http.extraheader="$auth" push -q origin "HEAD:$branch"; then
    echo "pushed to $branch"
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep $((i * 4))
done
echo "::error::could not push to $branch after 6 attempts"
exit 1
