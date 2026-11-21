import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import factory  # noqa: E402


def test_config_validates():
    assert factory.main(["validate"]) == 0


def test_all_prompts_render():
    cfg = factory.load_config()
    ctx = factory.build_context(cfg, {"repo": "o/r", "issue_number": "7", "base_branch": "main", "date": "2026-01-01"})
    for name, stage in cfg["workflows"].items():
        text = factory.render_template((ROOT / "factory" / stage["prompt"]).read_text(), ctx)
        assert "{{" not in text, name


@pytest.mark.parametrize("path,blocked", [
    (".github/workflows/ci.yml", True),
    ("app/.env", True),
    (".env.local", True),
    ("infra/prod/main.tf", True),
    ("src/secrets/key.py", True),
    ("src/app.py", False),
    ("infra/staging/main.tf", False),
])
def test_protected_paths(path, blocked):
    prot = factory.load_config()["policies"]["protected_paths"]
    assert bool(factory.evaluate_paths([path], prot, 25)) is blocked


def test_too_many_files():
    assert factory.evaluate_paths([f"f{i}.py" for i in range(30)], [], 25)


def test_access(capsys):
    assert factory.main(["check-access", "--repo", "ghifiardi/tantular-cyber", "--author-association", "MEMBER"]) == 0
    assert factory.main(["check-access", "--repo", "ghifiardi/tantular-cyber", "--author-association", "NONE"]) == 1
    assert factory.main(["check-access", "--repo", "other/repo"]) == 1


def test_redaction():
    s = "token ghp_abcdefghijklmnopqrstuvwxyz123456 and sk-ant-api03-SECRETSECRET and password=hunter22222"
    out = factory.redact(s)
    assert "ghp_" not in out and "sk-ant" not in out and "hunter2" not in out


def _exec_file(tmp_path, cost=0.42):
    msgs = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the issue"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "gh issue view 7"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "issue body", "is_error": False}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "boom"}], "is_error": True}]}},
        {"type": "result", "total_cost_usd": cost, "num_turns": 6, "duration_ms": 90000, "is_error": False, "result": "Opened PR"},
    ]
    p = tmp_path / "exec.json"
    p.write_text(json.dumps(msgs))
    return p


def test_record_session_and_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    data = tmp_path / "data"
    ex = _exec_file(tmp_path)
    now = factory.dt.datetime.now(factory.dt.timezone.utc).isoformat()
    for run in ("1", "2"):
        assert factory.main(["record-session", "--data-dir", str(data), "--workflow", "implement",
                             "--repo", "o/r", "--issue", "7", "--execution-file", str(ex), "--run-id", run,
                             "--conclusion", "success", "--started-at", now, "--pr-url", "https://x/pr/1"]) == 0
    recs = list(data.rglob("*.json"))
    assert len(recs) == 2
    rec = json.loads(recs[0].read_text())
    assert rec["metrics"]["cost_usd"] == 0.42
    assert rec["metrics"]["tool_calls"] == 2 and rec["metrics"]["tool_errors"] == 1
    assert factory.monthly_spend(data, factory.month_key()) == pytest.approx(0.84)
    assert factory.main(["check-budget", "--data-dir", str(data)]) == 0


def test_budget_hard_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    data = tmp_path / "data"
    ex = _exec_file(tmp_path, cost=600)
    factory.main(["record-session", "--data-dir", str(data), "--workflow", "implement", "--repo", "o/r",
                  "--issue", "1", "--execution-file", str(ex), "--run-id", "9"])
    assert factory.main(["check-budget", "--data-dir", str(data)]) == 1


def test_eval_loop(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    data = tmp_path / "data"
    ex = _exec_file(tmp_path)
    factory.main(["record-session", "--data-dir", str(data), "--workflow", "implement", "--repo", "o/r",
                  "--issue", "3", "--execution-file", str(ex), "--run-id", "5"])
    todo = tmp_path / "todo.txt"
    factory.main(["pending-evals", "--data-dir", str(data), "--out", str(todo)])
    pending = todo.read_text().split()
    assert len(pending) == 1
    ev_dir = data / "evals"; ev_dir.mkdir()
    (ev_dir / Path(pending[0]).name).write_text(json.dumps({
        "quality": 2, "summary": "added useless tests",
        "failure_modes": [{"id": "redundant_tests", "detected": True, "evidence": "test_constant"}]}))
    factory.main(["pending-evals", "--data-dir", str(data), "--out", str(todo)])
    assert todo.read_text().strip() == ""
    digest = tmp_path / "d.md"
    factory.main(["digest", "--data-dir", str(data), "--out", str(digest)])
    text = digest.read_text()
    assert "redundant_tests" in text and "100%" in text
    report = tmp_path / "r.md"
    factory.main(["metrics", "--data-dir", str(data), "--out", str(report), "--offline"])
    assert "implement sessions | 1" in report.read_text()


# ───────── hardening regressions ─────────
import subprocess
import yaml as _yaml


def _nul(tmp_path, names):
    p = tmp_path / "files.bin"
    p.write_bytes(b"\0".join(n.encode() for n in names) + b"\0")
    return str(p)


def test_files_from_is_not_split_on_commas(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    # One file whose NAME contains a comma: must be judged as a single path.
    f = _nul(tmp_path, [".github/workflows/x.yml,src/ok.py"])
    assert factory.main(["check-paths", "--files-from", f]) == 1


def test_improve_allowlist_rejects_outside_and_dotdot(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert factory.main(["check-paths", "--allowlist", "--files-from", _nul(tmp_path, ["factory/prompts/implement.md"])]) == 0
    assert factory.main(["check-paths", "--allowlist", "--files-from", _nul(tmp_path, ["factory/factory.yaml"])]) == 1
    assert factory.main(["check-paths", "--allowlist", "--files-from", _nul(tmp_path, ["scripts/factory.py"])]) == 1
    assert factory.main(["check-paths", "--allowlist", "--files-from", _nul(tmp_path, ["factory/prompts/../../scripts/x.py"])]) == 1


def test_allow_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert factory.main(["check-paths", "--allow", "evals/*.json", "--files-from", _nul(tmp_path, ["evals/a.json"])]) == 0
    assert factory.main(["check-paths", "--allow", "evals/*.json", "--files-from", _nul(tmp_path, ["sessions/a.json"])]) == 1


def test_rename_out_of_protected_path_is_caught(tmp_path):
    r = tmp_path / "repo"
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True)
    r.mkdir(); g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (r / ".github/workflows").mkdir(parents=True); (r / ".github/workflows/ci.yml").write_text("on: push\n" * 20)
    g("add", "-A"); g("commit", "-qm", "base"); g("switch", "-q", "-c", "agent")
    g("mv", ".github/workflows/ci.yml", "ci-moved.yml"); g("commit", "-qm", "move")
    files = factory.changed_files("main", "agent", str(r))
    assert ".github/workflows/ci.yml" in files
    prot = factory.load_config()["policies"]["protected_paths"]
    assert factory.evaluate_paths(files, prot, 25)


def test_validate_rejects_shell_for_improve(tmp_path):
    cfg = _yaml.safe_load((ROOT / "factory/factory.yaml").read_text())
    cfg["workflows"]["improve"]["allowed_tools"] += ",Bash(git:*)"
    p = tmp_path / "f.yaml"; p.write_text(_yaml.safe_dump(cfg))
    assert factory.main(["--config", str(p), "validate"]) == 1


def test_metrics_survives_unreadable_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    def boom(*a, **k):
        raise factory.urllib.error.HTTPError("u", 422, "Unprocessable", {}, None)
    monkeypatch.setattr(factory, "gh_api", boom)
    out = tmp_path / "r.md"
    assert factory.main(["metrics", "--data-dir", str(tmp_path / "d"), "--out", str(out)]) == 0
    assert "Data gaps" in out.read_text() and "422" in out.read_text()


def _workflow_python(workflow, step_name):
    wf = _yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    for job in wf["jobs"].values():
        for st in job.get("steps", []):
            if st.get("name") == step_name:
                body = st["run"]
                return body.split("<<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]
    raise KeyError(step_name)


def test_eval_publish_accepts_only_valid_pending_verdicts(tmp_path, monkeypatch):
    script = _workflow_python("factory-evaluate.yml", "Accept only valid verdicts for pending sessions")
    src, data = tmp_path / "in", tmp_path / "data"
    (src / "evals").mkdir(parents=True); data.mkdir()
    (src / "to_evaluate.txt").write_text("data/sessions/2026/11/o__r__1__9.json\n")
    good = {"quality": 4, "failure_modes": [{"id": "scope_creep", "detected": False, "evidence": "-"}]}
    (src / "evals/o__r__1__9.json").write_text(json.dumps(good))
    (src / "evals/not_pending.json").write_text(json.dumps(good))
    sp = tmp_path / "accept.py"; sp.write_text(script)
    monkeypatch.chdir(ROOT)
    out = subprocess.run([sys.executable, str(sp), str(src), str(data)], capture_output=True, text=True, check=True).stdout
    assert "accepted 1, rejected 1" in out
    assert (data / "evals/o__r__1__9.json").exists() and not (data / "evals/not_pending.json").exists()
    # bad schema / unknown failure mode is rejected
    (src / "evals/o__r__1__9.json").write_text(json.dumps({"quality": 9, "failure_modes": []}))
    (data / "evals/o__r__1__9.json").unlink()
    out = subprocess.run([sys.executable, str(sp), str(src), str(data)], capture_output=True, text=True, check=True).stdout
    assert "accepted 0" in out


def test_push_data_retries_and_keeps_token_off_disk(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "factory-data", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:factory-data"], check=True)
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        subprocess.run(["git", "clone", "-q", "-b", "factory-data", str(bare), str(d)], check=True, capture_output=True)
    (a / "one.json").write_text("1"); (b / "two.json").write_text("2")
    env = {**os.environ, "PUSH_TOKEN": "s3cr3t-token"}
    for d in (a, b):   # b is behind after a pushes → must rebase and succeed
        subprocess.run(["bash", str(ROOT / "scripts/push_data.sh"), str(d), "msg"], check=True, env=env, capture_output=True)
    log = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "--name-only", "factory-data"], capture_output=True, text=True).stdout
    assert "one.json" in log and "two.json" in log
    assert "s3cr3t" not in (b / ".git/config").read_text()


# ───────── pilot setup: repo settings, triage trust, log encryption ─────────
def _render(tmp_path, monkeypatch, repo):
    out = tmp_path / "out.txt"; out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert factory.main(["render", "--workflow", "implement", "--var", f"repo={repo}",
                         "--var", "issue_number=1", "--var", "base_branch=main"]) == 0
    return out.read_text()


def test_render_includes_repo_setup_and_checks(tmp_path, monkeypatch):
    out = _render(tmp_path, monkeypatch, "ghifiardi/tantular-cyber")
    assert "python_version=3.12" in out
    assert "pip install -e '.[dev,semgrep]'" in out
    assert "SEMGREP_SEND_METRICS=off" in out
    assert "`pytest -q`" in out and "`ruff check .`" in out
    assert "Bash(ruff:*)" in out


def test_render_without_repo_settings_is_generic(tmp_path, monkeypatch):
    out = _render(tmp_path, monkeypatch, "someone/else")
    assert "python_version=\n" in out and "setup_command=\n" in out
    assert "the project's test command" in out


def test_triage_trust(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    r = "ghifiardi/tantular-cyber"
    assert factory.main(["check-access", "--repo", r, "--author-association", "OWNER", "--stage", "triage"]) == 0
    assert factory.main(["check-access", "--repo", r, "--author-association", "NONE", "--stage", "triage"]) == 1
    assert factory.main(["check-access", "--repo", r, "--author-association", "NONE", "--stage", "implement"]) == 1


def test_triage_open_to_anyone_when_list_empty(tmp_path, monkeypatch):
    cfg = _yaml.safe_load((ROOT / "factory/factory.yaml").read_text())
    cfg["trust"]["triage_author_associations"] = []
    p = tmp_path / "f.yaml"; p.write_text(_yaml.safe_dump(cfg))
    assert factory.main(["--config", str(p), "check-access", "--repo", "ghifiardi/tantular-cyber",
                         "--author-association", "NONE", "--stage", "triage"]) == 0
    assert factory.main(["--config", str(p), "check-access", "--repo", "ghifiardi/tantular-cyber",
                         "--author-association", "NONE", "--stage", "implement"]) == 1


@pytest.mark.parametrize("key,val", [("PATH", "/x"), ("GITHUB_TOKEN", "x"), ("BASH_ENV", "x"), ("OK_NAME", "a\nb")])
def test_validate_rejects_bad_setup_env(tmp_path, key, val):
    cfg = _yaml.safe_load((ROOT / "factory/factory.yaml").read_text())
    cfg["repos"]["settings"]["ghifiardi/tantular-cyber"]["env"] = {key: val}
    p = tmp_path / "f.yaml"; p.write_text(_yaml.safe_dump(cfg))
    assert factory.main(["--config", str(p), "validate"]) == 1


def test_validate_rejects_settings_for_unlisted_repo(tmp_path):
    cfg = _yaml.safe_load((ROOT / "factory/factory.yaml").read_text())
    cfg["repos"]["settings"]["evil/repo"] = {"setup": "curl x | sh"}
    p = tmp_path / "f.yaml"; p.write_text(_yaml.safe_dump(cfg))
    assert factory.main(["--config", str(p), "validate"]) == 1


def _step(workflow, job, name):
    wf = _yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    for st in wf["jobs"][job]["steps"]:
        if st.get("name") == name:
            return st["run"]
    raise KeyError(name)


@pytest.mark.parametrize("workflow", ["factory-implement.yml", "factory-triage.yml"])
def test_log_encryption_roundtrip(tmp_path, workflow):
    priv = tmp_path / "priv.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(priv)],
                   check=True, capture_output=True)
    pub = subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout"], check=True, capture_output=True, text=True).stdout
    log = tmp_path / "exec.json"; log.write_text(json.dumps([{"type": "result", "total_cost_usd": 1.5}]) + "\n" * 3)
    rt = tmp_path / "rt"; rt.mkdir()
    out = tmp_path / "gh_out"; out.write_text("")
    env = {**os.environ, "RUNNER_TEMP": str(rt), "GITHUB_OUTPUT": str(out), "EXEC_FILE": str(log), "LOG_PUBLIC_KEY": pub}
    subprocess.run(["bash", "-e", "-c", _step(workflow, "agent", "Encrypt the execution log for the record job")],
                   check=True, env=env, capture_output=True)
    assert "ready=true" in out.read_text()
    enc = (rt / "factory-log/execution.json.enc").read_bytes()
    assert b"total_cost_usd" not in enc            # what gets uploaded is unreadable
    # record job: artifact downloaded to $RUNNER_TEMP/exec
    rt2 = tmp_path / "rt2"; (rt2 / "exec").mkdir(parents=True)
    for f in ("execution.json.enc", "key.enc"):
        (rt2 / "exec" / f).write_bytes((rt / "factory-log" / f).read_bytes())
    env2 = {**os.environ, "RUNNER_TEMP": str(rt2), "LOG_KEY": priv.read_text()}
    subprocess.run(["bash", "-e", "-c", _step(workflow, "record", "Decrypt the execution log")],
                   check=True, env=env2, capture_output=True)
    assert (rt2 / "exec/execution.json").read_text() == log.read_text()
    assert not (rt2 / "log_priv.pem").exists() and not (rt2 / "log_key.txt").exists()


def test_log_encryption_skips_without_public_key(tmp_path):
    rt = tmp_path / "rt"; rt.mkdir(); out = tmp_path / "o"; out.write_text("")
    log = tmp_path / "e.json"; log.write_text("{}")
    env = {**os.environ, "RUNNER_TEMP": str(rt), "GITHUB_OUTPUT": str(out), "EXEC_FILE": str(log), "LOG_PUBLIC_KEY": ""}
    subprocess.run(["bash", "-e", "-c", _step("factory-implement.yml", "agent", "Encrypt the execution log for the record job")],
                   check=True, env=env, capture_output=True)
    assert "ready=true" not in out.read_text() and not (rt / "factory-log/execution.json.enc").exists()


# ───────── public factory repo + private data repo ─────────
def _wf(path):
    d = _yaml.safe_load(Path(path).read_text())
    return d, d.get("on", d.get(True))   # PyYAML reads the bare key `on` as True


def _checkouts(wf):
    for job in wf["jobs"].values():
        for st in job.get("steps", []):
            if str(st.get("uses", "")).startswith("actions/checkout"):
                yield st.get("with", {})


def test_validate_requires_separate_data_repo(tmp_path):
    cfg = _yaml.safe_load((ROOT / "factory/factory.yaml").read_text())
    for bad in (None, cfg["factory_repo"]):
        c = dict(cfg)
        if bad is None:
            c.pop("data_repo")
        else:
            c["data_repo"] = bad
        p = tmp_path / "f.yaml"; p.write_text(_yaml.safe_dump(c))
        assert factory.main(["--config", str(p), "validate"]) == 1


@pytest.mark.parametrize("workflow", ["factory-implement.yml", "factory-triage.yml"])
def test_product_stages_keep_data_in_the_data_repo(workflow):
    wf, on = _wf(ROOT / ".github/workflows" / workflow)
    assert on["workflow_call"]["inputs"]["data_repo"]["required"] is True
    data = [w for w in _checkouts(wf) if w.get("ref") == "factory-data"]
    assert data and all(w["repository"] == "${{ inputs.data_repo }}" for w in data)


@pytest.mark.parametrize("workflow", ["factory-evaluate.yml", "factory-improve.yml", "factory-metrics.yml"])
def test_loops_are_reusable_and_run_from_the_data_repo(workflow):
    wf, on = _wf(ROOT / ".github/workflows" / workflow)
    # No schedule in the public repo: the private data repo's caller runs it, so its
    # artifacts, logs and report issues are private.
    assert set(on) == {"workflow_call"}
    for w in _checkouts(wf):
        if w.get("ref") == "factory-data":
            assert "repository" not in w            # caller = the data repo
        else:
            assert w["repository"] == "${{ inputs.factory_repo }}"


def test_improve_pr_goes_to_the_factory_repo():
    run = _step("factory-improve.yml", "publish", "Commit, push and open the PR")
    assert run.count('gh pr create --repo "$FACTORY_REPO"') == 2 and '--repo "$REPO"' not in run


def test_callers_match_the_reusable_workflows():
    cfg = _yaml.safe_load((ROOT / "factory/factory.yaml").read_text())
    callers = {"templates/factory.yml": None}
    callers.update({str(p.relative_to(ROOT)): None for p in (ROOT / "templates/data-repo/.github/workflows").glob("*.yml")})
    assert len(callers) == 4
    for path in callers:
        wf, _ = _wf(ROOT / path)
        for job in wf["jobs"].values():
            target = job["uses"]
            assert target.startswith(cfg["factory_repo"] + "/.github/workflows/") and target.endswith("@main")
            _, on = _wf(ROOT / ".github/workflows" / target.split("/")[-1].split("@")[0])
            call = on["workflow_call"]
            needed_in = {k for k, v in call.get("inputs", {}).items() if v.get("required")}
            assert needed_in <= set(job.get("with", {})), (path, needed_in)
            assert job["with"]["factory_repo"] == cfg["factory_repo"]
            if "data_repo" in job["with"]:
                assert job["with"]["data_repo"] == cfg["data_repo"]
            needed_sec = {k for k, v in (call.get("secrets") or {}).items() if v.get("required")}
            assert needed_sec <= set(job.get("secrets", {})), (path, needed_sec)
            assert job.get("secrets") != "inherit"
