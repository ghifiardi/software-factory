# Internal benchmarks (Phase 3)

Don't trust vendor claims about a new model, skill or harness — replay your own
history against it.

## How it works

1. **Build the task set.** Pick 20–50 closed issues from `factory-data/sessions`
   whose agent PR was merged with quality ≥ 4 (known-good answers) plus a
   handful that failed (known-hard). Record each as
   `benchmarks/tasks/<repo>__<issue>.yaml`:

   ```yaml
   repo: ghifiardi/tantular-cyber
   issue: 123
   base_sha: 3f2c1e…          # commit the original agent started from
   reference_pr: 456          # the merged human-approved solution
   test_command: "pytest -q"  # must fail at base_sha, pass at reference
   ```

2. **Define the configurations** to compare in `benchmarks/configs.yaml`, e.g.
   current production vs. a different model vs. a prompt change:

   ```yaml
   - name: prod
     model: claude-sonnet-5-5
   - name: opus
     model: claude-opus-5-5
   - name: haiku-cheap
     model: claude-haiku-5-5
   - name: new-implement-prompt
     model: claude-sonnet-5-5
     prompt_override: benchmarks/prompts/implement_v2.md
   ```

3. **Replay.** For each (task × config), check out `base_sha` in a fresh runner,
   run the implement stage *without* pushing (`--allowedTools` without `git push`/
   `gh pr create`), then run `test_command` and the online-eval agent on the result.

4. **Plot quality against cost.** For each config: pass rate, mean eval quality,
   mean cost, mean turns. You want the config at the top-left of the
   quality-vs-cost chart. Promote it by editing `factory/factory.yaml` in a PR
   and cite the benchmark run in the PR body.

Start this once the factory has ~50 recorded implement sessions; before that
the sample is too small to tell configurations apart.
