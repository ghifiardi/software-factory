# Software Factory — starter kit

A **code-defined, closed-loop system for running and continuously improving
cloud coding agents** across the org, built on GitHub Issues + GitHub Actions +
Claude Code. It follows the "Software Factory" model (Zach Lloyd, Warp): factories
as code, a closed SDLC loop, and measurement instead of vibes.

```
 Issue opened ──► TRIAGE (haiku) ──► label ready / needs-spec / blocked + spec
                                           │
             maintainer adds factory:implement
                                           ▼
          gates: repo allowlist · author trust · monthly budget
                                           ▼
                IMPLEMENT (sonnet, sandboxed runner) ──► PR (factory:agent-pr)
                                           ▼
          policy: protected paths · max files  ──► violating PR auto-closed
                                           ▼
            session record (cost, turns, tools, redacted transcript)
                                           ▼  factory-data branch (private data repo)
   daily  EVALUATE ──► per-session quality + failure modes
   weekly IMPROVE  ──► ONE PR against factory/prompts or factory/evals ─► human review
   weekly METRICS  ──► report issue: throughput, human touches/PR, cycle time, $/PR
```

## What maps to which layer

| Layer (from the talk) | Here |
|---|---|
| 6 · API & MCP first | GitHub Issues/labels are the interface; add Slack/Jira later as extra triggers |
| 5 · Orchestration | `templates/factory.yml` + reusable workflows in `.github/workflows/` |
| 4 · Inference & agents | `anthropics/claude-code-action@v1`; provider (Claude subscription · your own gateway · API key) and model per stage in `factory.yaml` |
| 3 · Compute & sandboxing | Ephemeral GitHub-hosted runners, scoped `--allowedTools`, no unrestricted Bash |
| 2 · Data & context | `factory-data` branch of the private data repo: `sessions/`, `evals/`, `reports/` |
| 1 · Factories as code | `factory/factory.yaml`, `factory/prompts/`, `factory/evals/` — changed only by PR |

## Two repos

| Repo | Visibility | Holds |
|---|---|---|
| `ghifiardi/software-factory` (this one) | **public** | `factory.yaml`, prompts, rubric, scripts, reusable workflows. No data, no secrets. |
| `ghifiardi/software-factory-data` | **private** | `factory-data` branch (session records with redacted transcripts, evals, reports), plus the schedules that run evaluate, improve and metrics, so their logs, artifacts and report issues stay private. |

The factory repo has to be public because a public product repo like `tantular-cyber` can only call
reusable workflows that live in a public repo. Anything sensitive lives in the data repo.

## Choosing the model provider

Set `inference.provider` in `factory/factory.yaml` (a PR, like any factory change). Each provider needs one
secret, in the data repo and every product repo:

| `inference.provider` | Secret | Notes |
|---|---|---|
| `claude-subscription` (default) | `CLAUDE_CODE_OAUTH_TOKEN` | Your Claude Pro/Max plan. Make the token with `claude setup-token`. Runs count against your plan's usage limits, shared with your own Claude use. |
| `gateway` | `GATEWAY_KEY` | Your own Anthropic-compatible gateway (`inference.gateway.url`), e.g. Bifrost in front of Ollama. All stages use `inference.gateway.model` unless a stage sets `gateway_model`. |
| `anthropic-api` | `ANTHROPIC_API_KEY` | Pay per call. Use a dedicated Console workspace with a spend limit. |

**Gateway notes.** Bifrost v2.2.4's `/anthropic` route keeps only the first system block, so the agent
would lose its instructions. `scripts/gateway_shim.py` runs on the runner, merges the blocks, and adds
`GATEWAY_KEY` as a Bearer token. Before switching, check a model can drive the agent: in testing,
`ollama/gpt-oss:120b` fixed a bug and ran the test; `ollama/qwen3-coder:30b` kept stopping after announcing
a step. The first call after the model is unloaded can take minutes.

**Budget.** `budget.monthly_session_limit` caps agent sessions per month for every provider. The dollar
limits count only `anthropic-api` sessions: subscription and gateway runs aren't billed per call, and the
cost Claude Code reports for them is an API-price estimate.

## Repo layout

```
factory/factory.yaml           policies, budgets, repos, stages, models  ← the factory
factory/prompts/*.md           stage prompts (templated with {{vars}})
factory/evals/failure_modes.yaml   online-eval rubric
.github/workflows/factory-*.yml    reusable stages + scheduled loops + CI
templates/factory.yml          the ONLY file a product repo needs
templates/data-repo/           loop schedules bootstrap.sh installs on the data repo's main
scripts/factory.py             gates, rendering, recording, digest, metrics (PyYAML only)
scripts/bootstrap.sh           one-time setup (data repo, data branch, loop callers, labels, log key)
scripts/push_data.sh           commit + push to factory-data with retries; token never written to disk
scripts/gateway_shim.py        local proxy for inference.provider: gateway (merges system blocks, adds GATEWAY_KEY)
tests/                         unit tests for the control script
benchmarks/                    Phase 3: replay history across configs
```

## Setup — `ghifiardi/tantular-cyber` pilot (≈1 hour)

Already configured in `factory/factory.yaml`: owner `ghifiardi`, pilot repo `tantular-cyber`
(Python 3.12, `pip install -e '.[dev,semgrep]'`, then the repo's own CI checks: ruff, pytest,
offline benchmark), a pilot budget of $30 soft / $50 hard per month, and triage limited to
trusted authors because the repo is public.

1. **Create the factory repo** `ghifiardi/software-factory` — **public** — and push this folder to `main`.
2. **Bootstrap** from the factory repo checkout:
   `./scripts/bootstrap.sh ghifiardi/software-factory ghifiardi/software-factory-data ghifiardi/tantular-cyber`
   It creates the private data repo with its `factory-data` branch, installs the evaluate/improve/metrics
   schedules on its `main` (evaluate and improve start disabled), makes the log-encryption key pair
   (private key stays in `~/.config/software-factory/`, back it up), sets `FACTORY_LOG_KEY` in the
   product repo, creates labels and lists any secret still missing.
   Then commit and push `factory/log_public_key.pem`.
3. **Create `FACTORY_TOKEN`**: a fine-grained personal access token, repository access =
   `software-factory` + `software-factory-data` + `tantular-cyber`, permissions *Contents: read & write*,
   *Pull requests: read & write*, *Issues: read*, *Metadata: read*.
4. **Add repo secrets** (personal accounts have no account-wide Actions secrets) in the **data repo
   and the product repo**. The public factory repo needs none. With the default provider
   (`claude-subscription`), run `claude setup-token` and copy the token it prints, then:
   ```bash
   pbpaste | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo ghifiardi/software-factory-data
   pbpaste | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo ghifiardi/tantular-cyber
   ```
   and the same for `FACTORY_TOKEN`. For another provider, see *Choosing the model provider*.
5. **Protect `main`** in the factory repo: require the `factory-validate` status check and require
   PRs. Don't require an approving review: GitHub won't let you approve your own PRs, so that rule
   would block every self-improvement PR. You review them yourself before merging.
6. **Install the Claude GitHub App** on `tantular-cyber` (https://github.com/apps/claude).
   PRs it opens trigger your normal CI; PRs opened with `GITHUB_TOKEN` would not.
7. **Wire the pilot repo:** copy `templates/factory.yml` → `tantular-cyber/.github/workflows/factory.yml`.
8. **Try it:** open a small, well-specified issue (e.g. a new Semgrep rule for one CWE with a
   vulnerable/clean fixture pair) → triage comments with a spec → add `factory:implement` →
   review the PR. Turn on `factory-evaluate` / `factory-improve` in the **data repo's** Actions tab
   once ~20 sessions are recorded.

To add a repo later: add it to `repos.allow` and `repos.settings` (PR), add it to `FACTORY_TOKEN`'s
repository access, set its two secrets, re-run `bootstrap.sh` with it (reuses the same key pair),
install the Claude app, copy the caller workflow.

Run checks locally any time:
```bash
pip install pyyaml pytest actionlint-py shellcheck-py
python scripts/factory.py validate && python -m pytest -q tests && actionlint .github/workflows/*.yml templates/factory.yml
```

## Day-2 operations

| You want to… | Do this |
|---|---|
| Add a repo | Add to `repos.allow` + `repos.settings` (PR), secrets, re-run bootstrap, copy the caller workflow |
| Change model / turns | Edit the stage in `factory.yaml` (PR) |
| Stop all agents now | Set `budget.monthly_hard_limit_usd: 0` (PR) or disable the workflows |
| Teach the factory a new failure | Add it to `factory/evals/failure_modes.yaml` |
| Roll back a bad prompt change | `git revert` the PR — factories as code |

## Security notes

**Agents never sit next to the factory's credentials.** Every stage is split into jobs:

| Stage | Agent job holds | Trusted job (fresh runner) does |
|---|---|---|
| triage / implement | the provider credential + a repo-scoped token for that product repo | gates, rendering (before); policy check, closing violating PRs, session record (after) — with `FACTORY_TOKEN` |
| evaluate | API key + read-only token; no shell | accepts only well-formed verdicts for sessions that were pending; pushes them |
| improve | API key + read-only token; no shell, no git | applies the agent's diff, rejects anything outside `improve.writable_paths`, then commits and opens the PR with `FACTORY_TOKEN` |

Other controls:
- **Untrusted input.** Issue text, transcripts and file names are treated as data: prompts say so, and
  workflows pass values to shell through environment variables, never by pasting `${{ }}` into commands.
  File lists are handled NUL-separated or as JSON, so odd file names can't split or inject.
- **Two trust gates for implement.** Only people with triage rights can add `factory:implement`, and
  only issues authored by OWNER/MEMBER/COLLABORATOR run. Triage also requires a trusted author by default (`trust.triage_author_associations`), and can only label and comment.
- **Enforced, not requested.** After every implement run, even when the agent errored or ran out of
  turns, PRs that touch `.github/workflows/**`, env files, secrets or `infra/prod/**` (including renames out of
  them), or exceed `max_files_changed`, are closed and their branch deleted.
- **No persisted credentials.** All checkouts use `persist-credentials: false`; pushes send the token as a
  per-command header (`scripts/push_data.sh`), so it is never in `.git/config`.
- **The factory can't approve itself.** Improve PRs are opened with `FACTORY_TOKEN`, so `factory-validate`
  runs on them and CODEOWNERS review is required.

Residual risks to accept knowingly:
- The implement agent runs your test suite, which means it can run code it wrote. Anything in that job's
  environment (the Anthropic key, the repo-scoped app token) is reachable by a determined prompt injection.
  Keep the trust gate on. With `claude-subscription` a stolen token spends your plan's limits; with
  `gateway` it uses your GPU; with `anthropic-api` use a dedicated Console workspace with a spend limit.
- Run logs from product repos leave the agent job **encrypted** (RSA-OAEP + AES-256) to
  `factory/log_public_key.pem`, because artifacts on public repos are downloadable by anyone; only
  the record job, holding `FACTORY_LOG_KEY`, can read them. Evaluate/improve/metrics run from the
  private data repo, so their logs, artifacts (kept up to 3 days) and report issues stay private.
  Only the stored session copy is redacted.
- Improve PRs are opened in the **public** factory repo. The improve prompt forbids quoting
  transcripts, issue text or product code; read the PR body before merging anyway.
- The agent job installs the project with `repos.settings.<repo>.setup` from `factory.yaml` — reviewed
  config, never issue text — so the agent can run the repo's real CI checks.
  Restrict who can read Actions artifacts in product repos.
- Transcript redaction is regex-based; keep the data repo private.
