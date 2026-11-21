#!/usr/bin/env python3
"""Software-factory control CLI.

One small, dependency-light tool (PyYAML only) used by every workflow:

  validate        check factory.yaml is well-formed and safe
  render          render a stage prompt + emit model/turns/tools to $GITHUB_OUTPUT
  check-access    is this repo / author allowed to use the factory?
  check-budget    has this month's recorded spend hit the hard limit?
  check-paths     does an agent branch touch protected paths / too many files?
  record-session  turn a claude-code-action execution file into a session record (Layer 2)
  pending-evals   list session records that have no eval yet
  digest          aggregate evals into eval_digest.md for the self-improvement agent
  metrics         build the weekly factory metrics report (markdown)
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "factory" / "factory.yaml"
BOT_SUFFIXES = ("[bot]",)
REQUIRED_STAGES = ("triage", "implement", "evaluate", "improve")


# ───────────────────────── helpers ─────────────────────────
def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def gh_output(**kv: str) -> None:
    """Write key=value pairs to $GITHUB_OUTPUT (or stdout when run locally)."""
    target = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for k, v in kv.items():
        v = str(v)
        if "\n" in v:
            delim = f"EOF_{k.upper()}"
            lines.append(f"{k}<<{delim}\n{v}\n{delim}")
        else:
            lines.append(f"{k}={v}")
    text = "\n".join(lines) + "\n"
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def lookup(ctx: dict, dotted: str):
    cur = ctx
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(dotted)
    return cur


def render_template(text: str, ctx: dict) -> str:
    def sub(m):
        val = lookup(ctx, m.group(1).strip())
        if isinstance(val, list):
            return ", ".join(f"`{v}`" for v in val)
        return str(val)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub, text)


def glob_match(path: str, pattern: str) -> bool:
    # fnmatch treats '*' as matching '/', so '**' and '*' both work across dirs;
    # also allow 'dir/**' to match 'dir' children and '**/x' to match top-level 'x'.
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    return False


def month_key(ts: str | None = None) -> str:
    d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else dt.datetime.now(dt.timezone.utc)
    return d.strftime("%Y-%m")


def iter_json(dirpath: Path):
    if not dirpath.exists():
        return
    for p in sorted(dirpath.rglob("*.json")):
        try:
            yield p, json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"warning: skipping unreadable {p}", file=sys.stderr)


def is_bot(login: str | None) -> bool:
    return bool(login) and login.endswith(BOT_SUFFIXES)


# ───────────────────────── validate ─────────────────────────
def cmd_validate(a) -> int:
    cfg = load_config(a.config)
    errs = []
    for key in ("version", "org", "factory_repo", "data_repo", "data_branch", "repos", "defaults", "budget", "policies", "workflows", "labels"):
        if key not in cfg:
            errs.append(f"missing top-level key: {key}")
    wf = cfg.get("workflows", {})
    for stage in REQUIRED_STAGES:
        if stage not in wf:
            errs.append(f"missing workflow stage: {stage}")
            continue
        prompt = ROOT / "factory" / wf[stage].get("prompt", "")
        if not prompt.is_file():
            errs.append(f"{stage}: prompt file not found: {prompt}")
        tools = wf[stage].get("allowed_tools", "")
        if re.search(r"(^|,)\s*Bash\s*(,|$)", tools) or "Bash(*)" in tools:
            errs.append(f"{stage}: unrestricted Bash is not allowed; scope it e.g. Bash(git:*)")
        if stage in ("evaluate", "improve") and "Bash" in tools:
            errs.append(f"{stage}: must not have shell access; it only edits files and a later job publishes them")
    b = cfg.get("budget", {})
    if b and b.get("monthly_soft_limit_usd", 0) > b.get("monthly_hard_limit_usd", 0):
        errs.append("budget: soft limit must be <= hard limit")
    prot = cfg.get("policies", {}).get("protected_paths", [])
    if ".github/workflows/**" not in prot:
        errs.append("policies.protected_paths must include .github/workflows/**")
    if cfg.get("data_repo") and cfg.get("data_repo") == cfg.get("factory_repo"):
        errs.append("data_repo must be a separate PRIVATE repo, not factory_repo (the factory repo is public)")
    if cfg.get("data_branch") != "factory-data":
        errs.append("data_branch must be 'factory-data' (the workflows use that name)")
    allow = cfg.get("repos", {}).get("allow", [])
    for repo, rs in (cfg.get("repos", {}).get("settings") or {}).items():
        if not any(fnmatch.fnmatch(repo, pat) for pat in allow):
            errs.append(f"repos.settings.{repo}: not in repos.allow")
        unknown = set(rs) - {"python", "node", "setup", "env", "checks", "extra_tools"}
        if unknown:
            errs.append(f"repos.settings.{repo}: unknown keys {sorted(unknown)}")
        et = rs.get("extra_tools", "")
        if re.search(r"(^|,)\s*Bash\s*(,|$)", et) or "Bash(*)" in et:
            errs.append(f"repos.settings.{repo}.extra_tools: unrestricted Bash is not allowed")
        for k, v in (rs.get("env") or {}).items():
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k) or k.startswith(("GITHUB_", "RUNNER_", "ACTIONS_")) \
                    or k in ("PATH", "HOME", "NODE_OPTIONS", "LD_PRELOAD", "BASH_ENV"):
                errs.append(f"repos.settings.{repo}.env: invalid or reserved name {k}")
            if "\n" in str(v) or "\r" in str(v):
                errs.append(f"repos.settings.{repo}.env.{k}: value must be a single line")
        if "\n" in str(rs.get("setup", "")):
            errs.append(f"repos.settings.{repo}.setup: use one line (join commands with &&)")
    imp = wf.get("improve", {})
    for p in imp.get("writable_paths", []):
        if not p.startswith("factory/prompts") and not p.startswith("factory/evals"):
            errs.append(f"improve.writable_paths may only cover factory/prompts or factory/evals, got {p}")
    # every template var in prompts must resolve with a representative context
    sample = build_context(cfg, {"repo": "o/r", "issue_number": "1", "base_branch": "main", "date": "2026-01-01"})
    for stage in REQUIRED_STAGES:
        if stage in wf:
            prompt = ROOT / "factory" / wf[stage]["prompt"]
            if prompt.is_file():
                try:
                    render_template(prompt.read_text(encoding="utf-8"), sample)
                except KeyError as e:
                    errs.append(f"{stage}: unknown template variable {e}")
    if errs:
        print("factory.yaml INVALID:\n  - " + "\n  - ".join(errs))
        return 1
    print("factory.yaml OK")
    return 0


# ───────────────────────── render ─────────────────────────
def build_context(cfg: dict, extra: dict) -> dict:
    ctx = {
        "org": cfg.get("org"),
        "labels": cfg.get("labels", {}),
        "protected_paths": cfg.get("policies", {}).get("protected_paths", []),
        "max_files_changed": cfg.get("policies", {}).get("max_files_changed", 25),
        "writable_paths": cfg.get("workflows", {}).get("improve", {}).get("writable_paths", []),
        "checks": "   - the project's test command (see README / CI)",
    }
    rs = repo_settings(cfg, extra.get("repo", ""))
    if rs.get("checks"):
        ctx["checks"] = "\n".join(f"   - `{c}`" for c in rs["checks"])
    ctx.update(extra)
    return ctx


def repo_settings(cfg: dict, repo: str) -> dict:
    return (cfg.get("repos", {}).get("settings") or {}).get(repo) or {}


def cmd_render(a) -> int:
    cfg = load_config(a.config)
    stage = cfg["workflows"][a.workflow]
    extra = dict(v.split("=", 1) for v in (a.var or []))
    extra.setdefault("date", dt.date.today().isoformat())
    prompt_text = render_template((ROOT / "factory" / stage["prompt"]).read_text(encoding="utf-8"), build_context(cfg, extra))
    model = stage.get("model", cfg["defaults"]["model"])
    turns = min(int(stage.get("max_turns", cfg["defaults"]["max_turns"])), int(cfg["budget"]["per_session_max_turns"]) if a.workflow == "implement" else 10**6)
    tools = stage["allowed_tools"]
    rs = repo_settings(cfg, extra.get("repo", ""))
    if a.workflow == "implement" and rs.get("extra_tools"):
        tools = f"{tools},{rs['extra_tools']}"
    claude_args = f'--model {model}\n--max-turns {turns}\n--allowedTools "{tools}"'
    gh_output(prompt=prompt_text, model=model, max_turns=turns, claude_args=claude_args,
              allowed_tools=tools, timeout_s=int(cfg["defaults"].get("timeout_minutes", 45)) * 60,
              timeout_minutes=cfg["defaults"].get("timeout_minutes", 45),
              python_version=rs.get("python", ""), node_version=rs.get("node", ""),
              setup_command=rs.get("setup", ""),
              setup_env="\n".join(f"{k}={v}" for k, v in (rs.get("env") or {}).items()))
    return 0


# ───────────────────────── checks ─────────────────────────
def cmd_check_access(a) -> int:
    cfg = load_config(a.config)
    repos = cfg.get("repos", {})
    if a.repo in repos.get("deny", []):
        print(f"DENY: {a.repo} is on the deny list"); return 1
    allow = repos.get("allow", [])
    if not any(fnmatch.fnmatch(a.repo, pat) for pat in allow):
        print(f"DENY: {a.repo} is not in repos.allow"); return 1
    if a.author_association is not None:
        trust = cfg.get("trust", {})
        if a.stage == "triage":
            ok = trust.get("triage_author_associations") or []
            if not ok:
                print(f"ALLOW: {a.repo} (triage accepts any author)"); return 0
        else:
            ok = trust.get("allowed_author_associations", [])
        if a.author_association not in ok:
            print(f"DENY: author association {a.author_association} not in {ok}"); return 1
    print(f"ALLOW: {a.repo}")
    return 0


def monthly_spend(data_dir: Path, month: str) -> float:
    total = 0.0
    for _, rec in iter_json(data_dir / "sessions"):
        if month_key(rec.get("started_at")) == month:
            total += float(rec.get("metrics", {}).get("cost_usd") or 0)
    return round(total, 4)


def cmd_check_budget(a) -> int:
    cfg = load_config(a.config)
    spend = monthly_spend(Path(a.data_dir), month_key())
    hard = cfg["budget"]["monthly_hard_limit_usd"]
    soft = cfg["budget"]["monthly_soft_limit_usd"]
    gh_output(spend_usd=spend)
    if spend >= hard:
        print(f"BUDGET EXCEEDED: ${spend:.2f} >= hard limit ${hard}"); return 1
    if spend >= soft:
        print(f"::warning::Factory spend ${spend:.2f} passed soft limit ${soft}")
    print(f"budget ok: ${spend:.2f} of ${hard}")
    return 0


def changed_files(base: str, head: str, repo_dir: str = ".") -> list[str]:
    # -z: exact names even with spaces/newlines; --no-renames: a rename reports both
    # old and new path, so moving a protected file out of place is still caught.
    out = subprocess.run(["git", "-C", repo_dir, "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}"],
                         check=True, capture_output=True, text=True).stdout
    return [f for f in out.split("\0") if f]


def read_file_list(path: str) -> list[str]:
    """NUL-separated list (git ... -z output). Never split on commas or newlines."""
    raw = Path(path).read_bytes().decode("utf-8", errors="replace")
    return [f for f in raw.split("\0") if f]


def evaluate_paths(files: list[str], protected: list[str], max_files: int) -> list[str]:
    problems = [f"protected path touched: {f}" for f in files if any(glob_match(f, p) for p in protected)]
    if len(files) > max_files:
        problems.append(f"{len(files)} files changed > max_files_changed {max_files}")
    return problems


def cmd_check_paths(a) -> int:
    cfg = load_config(a.config)
    pol = cfg["policies"]
    if a.files_from:
        files = read_file_list(a.files_from)
    elif a.files:
        files = a.files.split(",")
    else:
        files = changed_files(a.base, a.head, a.repo_dir)
    if a.allow:  # explicit allow-list mode (e.g. eval verdict files)
        problems = [f"outside allowed paths {a.allow}: {f!r}" for f in files
                    if not any(glob_match(f, p) for p in a.allow) or ".." in f.split("/")]
    else:
        protected = pol["protected_paths"] if not a.allowlist else []
        problems = evaluate_paths(files, protected, pol.get("max_files_changed", 25))
        if a.allowlist:  # inverse mode for the improve stage
            allow = cfg["workflows"]["improve"]["writable_paths"]
            problems += [f"outside improve.writable_paths: {f!r}" for f in files
                         if not any(glob_match(f, p) for p in allow) or ".." in f.split("/")]
    gh_output(files_changed=len(files), violations="\n".join(problems) or "none")
    if problems:
        print("POLICY VIOLATION:\n  - " + "\n  - ".join(problems)); return 1
    print(f"paths ok ({len(files)} files)")
    return 0


# ───────────────────────── record-session (Layer 2) ─────────────────────────
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def load_execution(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(l) for l in raw.splitlines() if l.strip()]


def summarise_execution(msgs: list[dict], max_chars: int = 600) -> tuple[dict, list[dict]]:
    metrics = {"cost_usd": None, "num_turns": None, "duration_ms": None, "tool_calls": 0,
               "tool_errors": 0, "is_error": None, "tools_used": {}}
    tools = Counter()
    transcript = []
    for m in msgs:
        t = m.get("type")
        if t == "result":
            metrics["cost_usd"] = m.get("total_cost_usd", m.get("cost_usd"))
            metrics["num_turns"] = m.get("num_turns")
            metrics["duration_ms"] = m.get("duration_ms")
            metrics["is_error"] = m.get("is_error")
            if m.get("result"):
                transcript.append({"role": "result", "text": redact(str(m["result"]))[:max_chars * 2]})
            continue
        content = (m.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            bt = block.get("type")
            if bt == "text" and t == "assistant":
                transcript.append({"role": "assistant", "text": redact(block["text"])[:max_chars]})
            elif bt == "tool_use":
                tools[block.get("name", "?")] += 1
                transcript.append({"role": "tool_use", "tool": block.get("name"),
                                   "input": redact(json.dumps(block.get("input", {})))[:max_chars]})
            elif bt == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                if block.get("is_error"):
                    metrics["tool_errors"] += 1
                transcript.append({"role": "tool_result", "error": bool(block.get("is_error")),
                                   "text": redact(str(body or ""))[:max_chars]})
    metrics["tool_calls"] = sum(tools.values())
    metrics["tools_used"] = dict(tools)
    return metrics, transcript


def cmd_record_session(a) -> int:
    msgs = load_execution(Path(a.execution_file)) if a.execution_file and Path(a.execution_file).exists() else []
    metrics, transcript = summarise_execution(msgs)
    now = dt.datetime.now(dt.timezone.utc)
    started = a.started_at or now.isoformat()
    rec = {
        "schema": 1,
        "session_id": a.session_id or f"{a.run_id}",
        "workflow": a.workflow,
        "repo": a.repo,
        "issue": int(a.issue) if a.issue else None,
        "pr_url": a.pr_url or None,
        "branch": a.branch or None,
        "conclusion": a.conclusion,
        "policy_violations": [v for v in (a.violations or "").splitlines() if v and v != "none"],
        "model": a.model,
        "run_url": a.run_url,
        "started_at": started,
        "recorded_at": now.isoformat(),
        "metrics": metrics,
        "diff_stat": a.diff_stat or "",
        "transcript": transcript,
    }
    d = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    safe_repo = a.repo.replace("/", "__")
    out = Path(a.data_dir) / "sessions" / d.strftime("%Y") / d.strftime("%m") / f"{safe_repo}__{a.issue or 'na'}__{a.run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print(f"recorded {out} (cost=${metrics['cost_usd']}, turns={metrics['num_turns']})")
    gh_output(session_file=str(out))
    return 0


# ───────────────────────── evals ─────────────────────────
def cmd_pending_evals(a) -> int:
    data = Path(a.data_dir)
    done = {p.name for p, _ in iter_json(data / "evals")}
    pending = [str(p) for p, rec in iter_json(data / "sessions")
               if p.name not in done and rec.get("workflow") == "implement"][: a.limit]
    Path(a.out).write_text("\n".join(pending) + ("\n" if pending else ""), encoding="utf-8")
    gh_output(count=len(pending))
    print(f"{len(pending)} session(s) pending evaluation")
    return 0


def load_evals(data: Path, days: int) -> list[dict]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    sessions = {p.name: r for p, r in iter_json(data / "sessions")}
    out = []
    for p, ev in iter_json(data / "evals"):
        s = sessions.get(p.name, {})
        ts = s.get("started_at")
        if ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
            continue
        ev["_session"] = s
        ev["_file"] = p.name
        out.append(ev)
    return out


def failure_stats(evals: list[dict], rubric: dict) -> list[tuple[str, int, float, int]]:
    sev = {fm["id"]: fm.get("severity", 1) for fm in rubric.get("failure_modes", [])}
    counts = Counter()
    for ev in evals:
        for fm in ev.get("failure_modes", []):
            if fm.get("detected"):
                counts[fm["id"]] += 1
    n = max(len(evals), 1)
    rows = [(fid, c, c / n, sev.get(fid, 1)) for fid, c in counts.items()]
    return sorted(rows, key=lambda r: r[2] * r[3], reverse=True)


def cmd_digest(a) -> int:
    data = Path(a.data_dir)
    rubric = yaml.safe_load((ROOT / "factory" / "evals" / "failure_modes.yaml").read_text(encoding="utf-8"))
    evals = load_evals(data, a.days)
    lines = [f"# Eval digest — last {a.days} days", "", f"Sessions evaluated: **{len(evals)}**"]
    if evals:
        q = [e.get("quality") for e in evals if isinstance(e.get("quality"), (int, float))]
        if q:
            lines.append(f"Average quality: **{sum(q)/len(q):.2f} / 5**")
        lines += ["", "## Failure modes (ranked by rate × severity)", "", "| failure mode | sessions | rate | severity |", "|---|---|---|---|"]
        for fid, c, rate, s in failure_stats(evals, rubric):
            lines.append(f"| {fid} | {c} | {rate:.0%} | {s} |")
        lines += ["", "## Worst sessions", ""]
        for e in sorted(evals, key=lambda e: e.get("quality", 5))[:8]:
            s = e["_session"]
            det = [f"{fm['id']}: {fm.get('evidence','')}" for fm in e.get("failure_modes", []) if fm.get("detected")]
            lines.append(f"- **{s.get('repo')}#{s.get('issue')}** quality {e.get('quality')} — {e.get('summary','')}"
                         f"  PR: {s.get('pr_url')}  file: `{e['_file']}`")
            lines += [f"    - {d}" for d in det]
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


# ───────────────────────── metrics ─────────────────────────
def gh_api(path: str, token: str) -> list | dict:
    results, url = [], f"https://api.github.com{path}"
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
            link = r.headers.get("Link", "")
        if isinstance(body, dict) and "items" in body:
            results.extend(body["items"])
        elif isinstance(body, list):
            results.extend(body)
        else:
            return body
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return results


def human_interactions(repo: str, number: int, token: str) -> int:
    """Human touches on an agent PR: reviews, review comments, conversation comments, human-authored commits."""
    n = 0
    for path in (f"/repos/{repo}/pulls/{number}/reviews", f"/repos/{repo}/pulls/{number}/comments",
                 f"/repos/{repo}/issues/{number}/comments"):
        n += sum(1 for x in gh_api(path + "?per_page=100", token) if not is_bot((x.get("user") or {}).get("login")))
    for c in gh_api(f"/repos/{repo}/pulls/{number}/commits?per_page=100", token):
        if not is_bot((c.get("author") or {}).get("login")) and (c.get("author") or {}).get("login"):
            n += 1
    return n


def hours_between(a: str, b: str) -> float:
    f = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (f(b) - f(a)).total_seconds() / 3600


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def cmd_metrics(a) -> int:
    cfg = load_config(a.config)
    data = Path(a.data_dir)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)
    sessions = [r for _, r in iter_json(data / "sessions")
                if r.get("started_at") and dt.datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")) >= since]
    impl = [s for s in sessions if s.get("workflow") == "implement"]
    cost = sum(float(s["metrics"].get("cost_usd") or 0) for s in sessions)
    with_pr = [s for s in impl if s.get("pr_url")]
    violations = [s for s in impl if s.get("policy_violations")]

    pr_rows, api_errors = [], []
    token = os.environ.get("GITHUB_TOKEN")
    if token and not a.offline:
        label = cfg["labels"]["agent_pr"]
        for repo in cfg["repos"]["allow"]:
            if "*" in repo:
                api_errors.append(f"{repo}: wildcard entries are skipped by the metrics report")
                continue
            q = f"repo:{repo} is:pr label:\"{label}\" created:>={since.date().isoformat()}"
            try:
                for pr in gh_api("/search/issues?per_page=100&q=" + urllib.parse.quote(q, safe=""), token):
                    merged_at = (pr.get("pull_request") or {}).get("merged_at")
                    pr_rows.append({
                        "repo": repo, "number": pr["number"], "state": pr["state"], "merged": bool(merged_at),
                        "cycle_h": hours_between(pr["created_at"], merged_at) if merged_at else None,
                        "human": human_interactions(repo, pr["number"], token),
                    })
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                # One unreadable repo (token scope, rename) must not kill the whole report.
                api_errors.append(f"{repo}: GitHub API error ({getattr(e, 'code', e)}) — check FACTORY_TOKEN can read this repo")
    elif not a.offline:
        api_errors.append("GITHUB_TOKEN not set — PR metrics skipped")
    merged = [p for p in pr_rows if p["merged"]]
    evals = load_evals(data, a.days)
    q = [e["quality"] for e in evals if isinstance(e.get("quality"), (int, float))]
    rubric = yaml.safe_load((ROOT / "factory" / "evals" / "failure_modes.yaml").read_text(encoding="utf-8"))
    month_spend = monthly_spend(data, month_key())
    b = cfg["budget"]

    fmt = lambda v, f="{:.1f}": "n/a" if v is None else f.format(v)
    L = [f"# Software factory report — last {a.days} days", "",
         "## Throughput", "",
         f"| metric | value |", "|---|---|",
         f"| implement sessions | {len(impl)} |",
         f"| sessions that opened a PR | {len(with_pr)} ({fmt(len(with_pr)/len(impl)*100 if impl else None,'{:.0f}%')}) |",
         f"| agent PRs (GitHub) | {len(pr_rows)} |",
         f"| merged agent PRs | {len(merged)} ({fmt(len(merged)/len(pr_rows)*100 if pr_rows else None,'{:.0f}%')} merge rate) |",
         f"| median PR open→merge (h) | {fmt(median([p['cycle_h'] for p in merged]))} |",
         f"| **human interactions per merged PR** (median) | {fmt(median([p['human'] for p in merged]))} |",
         f"| policy violations (auto-closed) | {len(violations)} |",
         "", "## Cost", "", "| metric | value |", "|---|---|",
         f"| spend in window | ${cost:.2f} |",
         f"| cost per merged PR | {fmt(cost/len(merged) if merged else None, '${:.2f}')} |",
         f"| month-to-date spend | ${month_spend:.2f} of ${b['monthly_hard_limit_usd']} hard limit"
         f"{' ⚠️ over soft limit' if month_spend >= b['monthly_soft_limit_usd'] else ''} |",
         f"| median turns per session | {fmt(median([s['metrics'].get('num_turns') for s in impl if s['metrics'].get('num_turns') is not None]), '{:.0f}')} |",
         "", "## Quality (online evals)", "",
         f"Sessions evaluated: {len(evals)} · average quality: {fmt(sum(q)/len(q) if q else None, '{:.2f}')} / 5", ""]
    stats = failure_stats(evals, rubric)
    if stats:
        L += ["| failure mode | rate |", "|---|---|"] + [f"| {fid} | {rate:.0%} |" for fid, _, rate, _ in stats[:6]]
    by_repo = defaultdict(lambda: [0, 0.0])
    for s in impl:
        by_repo[s["repo"]][0] += 1
        by_repo[s["repo"]][1] += float(s["metrics"].get("cost_usd") or 0)
    if by_repo:
        L += ["", "## By repo", "", "| repo | sessions | cost |", "|---|---|---|"]
        L += [f"| {r} | {n} | ${c:.2f} |" for r, (n, c) in sorted(by_repo.items())]
    if api_errors:
        L += ["", "## Data gaps", ""] + [f"- {e}" for e in api_errors]
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


# ───────────────────────── main ─────────────────────────
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("validate")
    r = sp.add_parser("render"); r.add_argument("--workflow", required=True); r.add_argument("--var", action="append")
    c = sp.add_parser("check-access"); c.add_argument("--repo", required=True); c.add_argument("--author-association")
    c.add_argument("--stage", choices=["triage", "implement"], help="which trust list to apply (default: implement)")
    c = sp.add_parser("check-budget"); c.add_argument("--data-dir", required=True)
    c = sp.add_parser("check-paths"); c.add_argument("--base"); c.add_argument("--head"); c.add_argument("--repo-dir", default=".")
    c.add_argument("--files", help="comma-separated list instead of git diff (tests/local use)")
    c.add_argument("--files-from", help="NUL-separated file list, e.g. from `git apply --numstat -z`")
    c.add_argument("--allowlist", action="store_true", help="improve-stage mode: only writable_paths allowed")
    c.add_argument("--allow", action="append", help="allow only paths matching this glob (repeatable)")
    s = sp.add_parser("record-session")
    for f in ("execution-file", "workflow", "repo", "issue", "pr-url", "branch", "conclusion", "violations",
              "model", "run-id", "run-url", "session-id", "started-at", "diff-stat"):
        s.add_argument(f"--{f}")
    s.add_argument("--data-dir", required=True)
    e = sp.add_parser("pending-evals"); e.add_argument("--data-dir", required=True); e.add_argument("--out", default="to_evaluate.txt"); e.add_argument("--limit", type=int, default=40)
    d = sp.add_parser("digest"); d.add_argument("--data-dir", required=True); d.add_argument("--days", type=int, default=30); d.add_argument("--out", default="eval_digest.md")
    m = sp.add_parser("metrics"); m.add_argument("--data-dir", required=True); m.add_argument("--days", type=int, default=7); m.add_argument("--out", default="factory_report.md"); m.add_argument("--offline", action="store_true")

    a = p.parse_args(argv)
    return {
        "validate": cmd_validate, "render": cmd_render, "check-access": cmd_check_access,
        "check-budget": cmd_check_budget, "check-paths": cmd_check_paths, "record-session": cmd_record_session,
        "pending-evals": cmd_pending_evals, "digest": cmd_digest, "metrics": cmd_metrics,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
