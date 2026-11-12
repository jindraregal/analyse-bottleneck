# Changelog

## 0.5.3 (2026-11-12)

Second round of fixes for the plugin directory's review.

- No download-and-run pattern in the rule files: the `poll_wait` rule that knew both a download tool and a shell started with `-c` is split into two rules (same matches).
- No credential-shaped examples: tests and rules use literal example hosts instead of environment variables, the CI export is described in words instead of a command, and the name rules for Coolify calls match the API path instead of a variable name.
- `js-npm-vs-pnpm-bun` no longer reads the project's npm config file (it can hold registry tokens); a `--prefer-offline` flag on the command still turns the rule off for that call.
- Nothing in the plugin needs a credential, so it declares no `userConfig`.

## 0.5.2 (2026-11-12)

Fixes for the plugin directory's review.

- No credentials from the user's machine: `dora` no longer calls `gh`; GitHub Actions runs come from a file you export yourself (`--ci-runs runs.json`, a JSON list of runs). The deep-dive subagent works from the computed facts, the project's files and deploy records you export; it is told not to look for or use tokens, logins or env values.
- No download-and-run commands: three tips no longer start a package that the project may not have installed, and a test fixture no longer pipes a download into an interpreter.
- Plugin icon (`icon` in plugin.json).

## 0.5.1 (2026-11-12)

Installable as a Claude Code plugin.

- `.claude-plugin/plugin.json` and a one-plugin `.claude-plugin/marketplace.json`: `/plugin marketplace add jindraregal/analyse-bottleneck`, then `/plugin install analyse-bottleneck@analyse-bottleneck`. Cloning into `~/.claude/skills/` works as before.
- SKILL.md points at its files with `${CLAUDE_SKILL_DIR}`, so the same commands work in both installs.
- Reports default to `~/.local/share/analyse-bottleneck/reports/` (`$XDG_DATA_HOME` if set) instead of `reports/` inside the skill folder, which a plugin update replaces.

## 0.5.0 (2026-11-12)

The overview shows what each slice can save, and the waiting slice has tips for the person.

- Overview next to the chart: every slice shows its time and "Can save" (its coded rules' estimates, capped at the slice's time; "–" where no rule has an honest estimate) instead of its share of the ring. The same number is under a picked slice's time in the chart's centre, in its tooltip and in the panel head. The "to save" stat and the "All slices" row are the column added up, waiting included.
- Waiting tips carry `for`: the page lists "What you can do" and "What the agent can do" apart (up to 3 each), and the first tip for you always makes "Do this first". New tip for you: `wait-push-on-stop` (a Stop or Notification hook sends a push when the agent waited a median 5 min or more for your next message; Claude Code hooks docs). The closing-question tip is now one for you: list the decisions agents may take alone in CLAUDE.md.
- Waiting estimates from measured time (`saving_basis`): the time after stops that ended with a question, or (waits - sessions) x median for one approval or one round of questions per session. On two real projects: ≈10 min and ≈2 min; no estimate where there is no honest one.

## 0.4.1 (2026-11-12)

Deep-dive checks fixed after the first runs on real projects.

- The cache-mount check is skipped when deploys build on Cloud Build (`unless_platforms`): its workers start empty, so a BuildKit cache mount does nothing there; `cloudbuild-cache` covers it.
- Copy order: when the install step already has a cache mount, sources copied before the install are listed as "Minor" under the deploy files and not raised as advice (a rerun only relinks packages). New check option `minor_if`.
- "Deploy once per batch": when the deep dive ran, the estimate uses the measured share of deploys that started within 30 min of the previous one's end instead of a fixed half (only ever lower). On a real project: 1.7 h → 1.4 h, the upper bound the verified deep dive found. New tip option `same_as_share`.

## 0.4.0 (2026-11-12)

The page is simpler, opens on all slices, and deploy waits get their own analysis.

- Header: the title, one plain sentence, four small stats with icons (to save, recommendations, period, sessions) and "How to read this", which explains every number and badge. No AI text up there; agent time and the waiting sit in the donut's centre.
- The page opens on "All slices": "Do this first" across slices (the 4 biggest rule savings, then the AI notes, then the rest) and any deep dive. Picking the selected slice again, the donut's centre, "Show all" or Esc goes back to All.
- Every list (Do this, What exactly, By task, deep dive, What this is based on, the waiting parts) is the same expandable row; each panel has "Expand all".
- Deep dives (`rules/deepdive.json`, `rbn/deepdive.py`): when deploys take at least 30 min and 10% of agent time (or 3 h), the script collects the deploy work from every slice, counts deploys, status checks per deploy, back-to-back deploys and foreground runs, names the platform, and checks the project's files on disk (BuildKit cache mounts, lockfile-first layer order, `.dockerignore`, Cloud Build `--cache-from`). Coded tips fire on these facts; a tip that repeats a slice rule is left out. `view` writes `<name>.deepdive-<id>.md`, the prompt for a read-only subagent; its notes merge under `deepdive:<id>`.
- Rules can carry a `source` (shown as "Source" under the tip). New rule `format-by-hand`: a formatter run by hand after edits, fix a PostToolUse hook (Claude Code best practices). Test rule knows `jest --onlyFailures`; the CI poll rule suggests `gh pr checks --watch --fail-fast`; the compaction advice now says `/clear` between unrelated tasks instead of "delegate to subagents" (listed as outdated in the Claude Code best practices).

## 0.3.2 (2026-11-11)

Fixed false alarms from stack rules that looked only at the command text.

- `ts-tsc-not-incremental` no longer fires when the project is already incremental: the tsconfig the call uses sets `incremental` or `composite` (also inherited through `extends`, including configs from packages, and through project references), or a `*.tsbuildinfo` exists next to it, in `outDir` or at `tsBuildInfoFile`.
- `js-npm-vs-pnpm-bun` no longer fires when `.npmrc` sets `prefer-offline=true`.
- `next-turbopack-not-used` no longer fires when `package.json` has Next 16 or newer, where Turbopack is the default.
- New detector option `skip_if_project` (`tsconfig`, `file_regex`, `dep_min_major`) and module `rbn/repo.py`. Each call's working directory is kept; `cd`, `--filter`/`-C` and `-p` are followed. A directory that is gone (deleted worktree, temp clone) falls back to the project's main checkout; an unknown one changes nothing.
- `meta.project_checks` in the JSON report lists the calls left out, per rule, with the reason.

## 0.3.1 and earlier

See the git history.
