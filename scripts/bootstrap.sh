#!/usr/bin/env bash
# One-time setup (safe to re-run). Run from the root of the factory repo checkout:
#   ./scripts/bootstrap.sh ghifiardi/software-factory ghifiardi/software-factory-data ghifiardi/tantular-cyber [more...]
#     1st arg: the PUBLIC factory repo (code, prompts, config, reusable workflows)
#     2nd arg: the PRIVATE data repo (factory-data branch + evaluate/improve/metrics callers); created if missing
#     rest:    product repos
# Requires: gh CLI logged in as the owner, git and openssl. Works for personal accounts and orgs.
set -euo pipefail

usage="usage: bootstrap.sh OWNER/factory-repo OWNER/data-repo OWNER/product-repo..."
FACTORY_REPO="${1:?$usage}"; DATA_REPO="${2:?$usage}"; shift 2
PRODUCT_REPOS=("$@")
[ ${#PRODUCT_REPOS[@]} -gt 0 ] || { echo "$usage"; exit 1; }
KEY_DIR="${FACTORY_KEY_DIR:-$HOME/.config/software-factory}"
PRIV_KEY="$KEY_DIR/log_private_key.pem"
PUB_KEY="factory/log_public_key.pem"

[ -f factory/factory.yaml ] || { echo "run this from the root of the factory repo checkout"; exit 1; }
grep -qE "^data_repo: *$DATA_REPO( |$)" factory/factory.yaml \
  || { echo "data_repo in factory/factory.yaml is not $DATA_REPO — fix one of them"; exit 1; }
[ "$DATA_REPO" != "$FACTORY_REPO" ] || { echo "the data repo must be separate from the factory repo"; exit 1; }

# gh supplies the credential; the token never appears in argv or .git/config.
gitgh() { git -c credential.helper= -c 'credential.helper=!gh auth git-credential' "$@"; }

echo "▸ Factory repo $FACTORY_REPO must be public (public product repos can only call public workflows)"
vis=$(gh api "repos/$FACTORY_REPO" --jq .visibility)
if [ "$vis" != "public" ]; then
  echo "  ! it is $vis. Make it public once it holds no data: gh repo edit $FACTORY_REPO --visibility public --accept-visibility-change-consequences"
fi

echo "▸ Data repo $DATA_REPO (private)"
if ! gh api "repos/$DATA_REPO" --silent 2>/dev/null; then
  gh repo create "$DATA_REPO" --private --description "Software factory data: session records, evals, reports. Private." >/dev/null
  echo "  created"
fi
vis=$(gh api "repos/$DATA_REPO" --jq .visibility)
[ "$vis" = "private" ] || { echo "  ! $DATA_REPO is $vis — it holds transcripts and must be private"; exit 1; }

echo "▸ Loop callers on $DATA_REPO main (evaluate · improve · metrics)"
first_install=false
tmp=$(mktemp -d)
if gh api "repos/$DATA_REPO/branches/main" --silent 2>/dev/null; then
  gitgh clone -q --depth 1 -b main "https://github.com/$DATA_REPO.git" "$tmp"
else
  first_install=true
  git -C "$tmp" init -q -b main
  # shellcheck disable=SC2016  # backticks are Markdown, not command substitution
  printf '# %s\n\nPrivate data repo of the software factory (%s).\n\n- `factory-data` branch: session records, evals, reports. Written by the factory; do not edit by hand.\n- `.github/workflows/`: schedules for the evaluate, improve and metrics loops, which run here so their logs stay private.\n' \
    "$DATA_REPO" "$FACTORY_REPO" > "$tmp/README.md"
fi
mkdir -p "$tmp/.github/workflows"
cp templates/data-repo/.github/workflows/*.yml "$tmp/.github/workflows/"
git -C "$tmp" add -A
if git -C "$tmp" diff --cached --quiet; then
  echo "  up to date"
else
  git -C "$tmp" -c user.name=factory -c user.email=factory@noreply commit -qm "factory: loop callers"
  gitgh -C "$tmp" push -q "https://github.com/$DATA_REPO.git" HEAD:main
  echo "  pushed"
fi
rm -rf "$tmp"
if $first_install; then
  # Nothing to evaluate or improve until sessions exist: start these two switched off.
  sleep 5
  for w in factory-evaluate.yml factory-improve.yml; do
    gh workflow disable "$w" --repo "$DATA_REPO" >/dev/null 2>&1 && echo "  $w disabled (enable after ~20 sessions)" \
      || echo "  ! disable $w by hand in $DATA_REPO → Actions until ~20 sessions are recorded"
  done
fi

echo "▸ Data branch 'factory-data' in $DATA_REPO (Layer 2 store)"
# Ask the API (authenticated via gh) — an anonymous ls-remote always fails on a
# private repo and would make a re-run try to recreate the branch.
if gh api "repos/$DATA_REPO/branches/factory-data" --silent 2>/dev/null; then
  echo "  already exists"
else
  tmp=$(mktemp -d)
  git -C "$tmp" init -q -b factory-data
  mkdir -p "$tmp/sessions" "$tmp/evals" "$tmp/reports"
  touch "$tmp/sessions/.keep" "$tmp/evals/.keep" "$tmp/reports/.keep"
  printf '# factory-data\nSession records, eval results and reports written by the factory. Do not edit by hand.\n' > "$tmp/README.md"
  git -C "$tmp" add -A && git -C "$tmp" -c user.name=factory -c user.email=factory@noreply commit -qm "init factory-data"
  gitgh -C "$tmp" push -q "https://github.com/$DATA_REPO.git" factory-data
  rm -rf "$tmp"
fi

echo "▸ Log-encryption key pair (run logs leave the agent job encrypted)"
mkdir -p "$KEY_DIR"; chmod 700 "$KEY_DIR"
if [ ! -f "$PRIV_KEY" ]; then
  if [ -f "$PUB_KEY" ]; then
    echo "  ! $PUB_KEY exists but $PRIV_KEY does not."
    echo "    Point FACTORY_KEY_DIR at the folder holding the private key, or delete $PUB_KEY to make a new pair."
    exit 1
  fi
  (umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out "$PRIV_KEY" 2>/dev/null)
  echo "  new private key: $PRIV_KEY  (back it up; it never goes into git)"
fi
openssl pkey -in "$PRIV_KEY" -pubout -out "$PUB_KEY"
echo "  public key written to $PUB_KEY — commit and push it if it changed"

label() { gh label create "$2" --repo "$1" --color "$3" --description "$4" --force >/dev/null; }
for repo in "${PRODUCT_REPOS[@]}"; do
  echo "▸ Labels in $repo"
  label "$repo" "factory:implement"        "5319e7" "Send this issue to the software factory"
  label "$repo" "factory:ready"            "0e8a16" "Triaged: spec is clear enough for an agent"
  label "$repo" "factory:needs-spec"       "fbca04" "Triaged: needs clarification"
  label "$repo" "factory:blocked"          "b60205" "Not suitable for the factory / policy violation"
  label "$repo" "factory:agent-pr"         "1d76db" "PR opened by a factory agent"
done
echo "▸ Labels in $FACTORY_REPO and $DATA_REPO"
label "$FACTORY_REPO" "factory:self-improvement" "c5def5" "Factory proposing a change to itself"
label "$DATA_REPO"    "factory:report"           "ededed" "Weekly factory metrics"

for repo in "${PRODUCT_REPOS[@]}"; do
  echo "▸ FACTORY_LOG_KEY secret in $repo"
  gh secret set FACTORY_LOG_KEY --repo "$repo" < "$PRIV_KEY"
done

# The public factory repo needs no secrets: everything that spends or writes runs
# from the data repo or a product repo.
provider=$(sed -n 's/^  provider: *\([a-z-]*\).*/\1/p' factory/factory.yaml | head -1)
case "$provider" in
  claude-subscription) cred=CLAUDE_CODE_OAUTH_TOKEN ;;   # from `claude setup-token`
  gateway)             cred=GATEWAY_KEY ;;
  *)                   cred=ANTHROPIC_API_KEY ;;
esac
echo "▸ inference.provider is ${provider:-anthropic-api}: agents need $cred"
missing=()
for repo in "$DATA_REPO" "${PRODUCT_REPOS[@]}"; do
  have=$(gh secret list --repo "$repo" --json name --jq '.[].name' 2>/dev/null || true)
  for s in "$cred" FACTORY_TOKEN; do
    grep -qx "$s" <<< "$have" || missing+=("$repo:$s")
  done
done

cat <<EOF

Done. Remaining steps (README → Setup):
  1. Commit + push $PUB_KEY to $FACTORY_REPO
  2. Install the Claude GitHub App on: ${PRODUCT_REPOS[*]}   https://github.com/apps/claude
  3. Copy templates/factory.yml → .github/workflows/factory.yml in each product repo
EOF
if [ ${#missing[@]} -gt 0 ]; then
  echo "  4. Add these missing secrets (gh secret set NAME --repo REPO):"
  printf '       %s\n' "${missing[@]}"
fi
