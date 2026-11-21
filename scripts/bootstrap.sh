#!/usr/bin/env bash
# One-time setup (safe to re-run). Run from the root of the factory repo checkout:
#   ./scripts/bootstrap.sh ghifiardi/software-factory ghifiardi/tantular-cyber [more product repos...]
# Requires: gh CLI logged in as the owner, and openssl. Works for personal accounts and orgs.
set -euo pipefail

FACTORY_REPO="${1:?usage: bootstrap.sh OWNER/factory-repo OWNER/product-repo...}"; shift
PRODUCT_REPOS=("$@")
OWNER="${FACTORY_REPO%%/*}"
KEY_DIR="${FACTORY_KEY_DIR:-$HOME/.config/software-factory}"
PRIV_KEY="$KEY_DIR/log_private_key.pem"
PUB_KEY="factory/log_public_key.pem"

[ -f factory/factory.yaml ] || { echo "run this from the root of the factory repo checkout"; exit 1; }

echo "▸ Data branch 'factory-data' in $FACTORY_REPO (Layer 2 store)"
# Ask the API (authenticated via gh) — an anonymous ls-remote always fails on a
# private repo and would make a re-run try to recreate the branch.
if gh api "repos/$FACTORY_REPO/branches/factory-data" --silent 2>/dev/null; then
  echo "  already exists"
else
  tmp=$(mktemp -d)
  git -C "$tmp" init -q -b factory-data
  mkdir -p "$tmp/sessions" "$tmp/evals" "$tmp/reports"
  touch "$tmp/sessions/.keep" "$tmp/evals/.keep" "$tmp/reports/.keep"
  printf '# factory-data\nSession records, eval results and reports written by the factory. Do not edit by hand.\n' > "$tmp/README.md"
  git -C "$tmp" add -A && git -C "$tmp" -c user.name=factory -c user.email=factory@noreply commit -qm "init factory-data"
  # gh supplies the credential; the token never appears in argv or .git/config.
  git -C "$tmp" -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
    push -q "https://github.com/$FACTORY_REPO.git" factory-data
  rm -rf "$tmp"
fi

echo "▸ Letting your other repos call the reusable workflows in $FACTORY_REPO"
owner_type=$(gh api "users/$OWNER" --jq .type)
level=$([ "$owner_type" = "Organization" ] && echo organization || echo user)
if gh api -X PUT "repos/$FACTORY_REPO/actions/permissions/access" -f access_level="$level" >/dev/null; then
  echo "  access_level=$level"
else
  echo "  ! set manually: $FACTORY_REPO → Settings → Actions → General → Access"
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
for repo in "$FACTORY_REPO" "${PRODUCT_REPOS[@]}"; do
  echo "▸ Labels in $repo"
  label "$repo" "factory:implement"        "5319e7" "Send this issue to the software factory"
  label "$repo" "factory:ready"            "0e8a16" "Triaged: spec is clear enough for an agent"
  label "$repo" "factory:needs-spec"       "fbca04" "Triaged: needs clarification"
  label "$repo" "factory:blocked"          "b60205" "Not suitable for the factory / policy violation"
  label "$repo" "factory:agent-pr"         "1d76db" "PR opened by a factory agent"
done
label "$FACTORY_REPO" "factory:self-improvement" "c5def5" "Factory proposing a change to itself"
label "$FACTORY_REPO" "factory:report"           "ededed" "Weekly factory metrics"

for repo in "${PRODUCT_REPOS[@]}"; do
  echo "▸ FACTORY_LOG_KEY secret in $repo"
  gh secret set FACTORY_LOG_KEY --repo "$repo" < "$PRIV_KEY"
done

missing=()
for repo in "$FACTORY_REPO" "${PRODUCT_REPOS[@]}"; do
  have=$(gh secret list --repo "$repo" --json name --jq '.[].name' 2>/dev/null || true)
  for s in ANTHROPIC_API_KEY FACTORY_TOKEN; do
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
