# analyse-bottleneck

A Claude Code skill that shows where the wall-clock time of your agent runs went (tests, builds, deploys, polling, model time, waiting for you) and ranks the fixes. Free and MIT licensed; it runs locally on the transcripts Claude Code already keeps.

```bash
npx skills add jindraregal/analyse-bottleneck            # this project
npx skills add jindraregal/analyse-bottleneck --global   # or every project
```

Then ask Claude Code: "use analyse-bottleneck on my last 24 hours". Plugin install and other options are under [Install](#install).

**Where did the time and the quota of your long autonomous Claude Code runs really go, and what do you change to make the next run faster?**

`analyse-bottleneck` is a Claude Code skill plus a zero-dependency Python tool. It reads the transcripts Claude Code already writes to `~/.claude/projects`, attributes every second of wall-clock time to a cause, finds quota waste and stack-specific bad habits, and hands the model a compact report with ranked fixes: evidence, before/after commands, estimated savings with the arithmetic shown.

About 90% of the work is plain code over the transcript data. The model is a thin layer on top: it reads roughly 80 lines of markdown, applies judgement, and writes recommendations. No brute-force inference over megabytes of logs, so it is cheap on a tight weekly quota.

## What you get

- **One chart (`view`).** On top the title, one plain sentence, four small stats with icons (possible saving, number of recommendations, period, sessions) and "How to read this", which explains each number and badge. Then one donut: agent time by cause plus a hatched slice "Waiting for you", with agent time and the waiting in its centre. Next to it the overview lists every slice with its time and what it can save ("Can save": the coded rules' estimates, capped at the slice's time; "–" where no rule has an honest estimate), not its share. The page opens on "All slices": "Do this first" across slices (the biggest rule savings, then the AI notes) and any deep dive. Every list on the page is the same expandable row, each panel has "Expand all", and picking the selected slice again (or the centre, "Show all", Esc) goes back to All. Each agent slice is a cause (tests, builds, installs, poll loops, model time, network/git/APIs, browser, media, servers, scripts, files, other, "cause unknown"); clicking it shows "Do this" (coded actions, optional AI notes) and "What exactly": each item has a 2-5 word name (`rules/describe.json`, else the agent's own description of the call, cut to its first clause) and opens to the command, the agent's note, who ran it, the median run and the sessions. Where the listed items explain less than 70% of a slice, a short "By task" list adds the hours per main session, subagent or workflow task. One-off scratch or temp scripts of one task become one item when that explains more; poll loops are grouped by what they wait for. Waits on subagents and background tasks are resolved into what the child or the background command was doing at that moment, recursively; what cannot be resolved is "cause unknown". The hatched slice splits the time the main sessions waited for a person into your next message (and how many of those turns ended with a question), permission prompts (and which commands), approval gates, questions and parked sessions, with tips from `rules/waiting.json`, split into what you can do (allow-rules, a list of decisions agents may take alone, a push when an agent stops, follow-up tasks up front) and what the agent can do (one approval per task, questions in one batch). The page layout, names and icons are all computed by the script, so every report looks the same; a model only adds short notes per slice in a JSON file. Next to it a `brief.md` (<= 60 lines) for a model to read.
- **Deep dives.** When one cause takes a big share of agent time (today: deploys, at least 30 min and 10% of agent time or 3 h), the script runs a focused second analysis across all slices (a deploy is run in "Deploys", waited on in "Sleep/poll loops" and checked in "Network"): deploys as episodes, status checks per deploy, deploys started right after the previous one, foreground runs, the platform, and checks of the project's files as they are today (Dockerfile cache mounts and layer order, `.dockerignore`, Cloud Build cache). Coded tips fire on these facts. It also writes `<name>.deepdive-<id>.md`, the prompt for a read-only subagent that works from those facts, the project's files and any deploy records you export for it (it never uses tokens or logins found on the machine); its notes come back under `deepdive:<id>` with an AI badge. Defined in `rules/deepdive.json`.
- **Wall-clock attribution that sums to 100%.** Per session, an interval-union sweep assigns each second to exactly one of: model inference, thinking, build, test, install, lint/typecheck, git, network, web, MCP (per server), browser automation, media/GPU jobs, file ops, polling/sleep, subagent wait, background-task wait, API wait (stalls, retries). Parallel tools and subagents are never double counted. Human and permission waits and idle gaps are separated and excluded from the autonomous time.
- **Environment signals** parsed from tool output: disk full, OOM/exit 137/heap exhaustion, timeouts, CPU/process pressure, missing GPU, CUDA OOM, network errors, DNS, rate limits (429), overload (529), permission denials, retry loops, compactions, heavy jobs overlapping (also across parallel sessions). Each carries a confidence label.
- **Quota waste** in weighted units: tokens per model, cache hit rate, cache rebuilds (idle past the TTL vs changed prefix), oversized tool outputs and how long they were carried, repeated reads, repeated identical commands (same output, no edit between), failing retry loops, sleep polling, top-tier model on mechanical turns, many tiny subagents, screenshots where a text snapshot would do, loops run by the model that code should run.
- **Stack-waste rules** as data in `rules/*.json`, each with a detector, an evidence threshold, why, fix, before/after, and stated estimate arithmetic. Included: Next.js (full `next build` as verification, wiped `.next`, webpack/no Turbopack, dev-server restarts, heap OOM, reinstalling dependencies per worktree), npm vs pnpm/bun, pip vs uv, webpack vs rspack/vite, jest vs vitest, eslint/prettier vs biome, tsc without incremental or tsgo, full test suite vs affected, Playwright full reruns, Docker without cache/BuildKit, grep/find vs rg/fd, cargo/gradle/go cache wipes, ffmpeg without hardware encoding, reference whisper vs faster-whisper/GPU, repeated large clones, CI polled by `sleep`.
- **calls** lists every tool call and model turn: how long it lasted and what it likely did (HTML page and CSV).
- **compare** two windows or sessions per autonomous hour, to prove an optimisation helped.
- **Measured telemetry next to the inference**: API latency per request (send record -> first and last content block, by model, by context size, first request of a session), Stop-hook overhead, durations the tools print about themselves (test runners, dev-server ready time, installs, `time`), and optionally host CPU/iowait/steal/memory/disk utilisation from **sysstat** archives (`--sysstat`) and queue/admission waits and memory peaks from a job wrapper's records (`--jobs`).
- **A coverage matrix** in every report: for each dimension (CPU, memory, disk I/O, disk space, GPU, API latency, tokens, polling, subagents, builds/tests, cold starts, queues, hooks, CI/DORA) whether the numbers are measured, inferred, a snapshot, or missing, and from what source.
- **dora**: delivery metrics from git, GitHub Actions runs you export (`--ci-runs`) and an optional deploy log. Each value is labelled measured or proxy; a metric with no source is reported missing.
- **calls** lists every tool call and model turn: how long it lasted and what it likely did (HTML page and CSV).
- **compare** two windows or sessions per autonomous hour, to prove an optimisation helped.
- **watch**, an opt-in Stop hook for continuous observation: non-blocking, rate-limited, auditable, with an on/off switch. Never installed for you (see below).
- **sample_machine.py**: optional sampler for load, CPU, memory, disk, network, GPU, so inferred machine signals can be corroborated with measurements.

## Install

For Claude Code: it reads the session transcripts Claude Code keeps in `~/.claude`. Python 3.8+ with the standard library only.

As a plugin, inside Claude Code:

```
/plugin marketplace add jindraregal/analyse-bottleneck
/plugin install analyse-bottleneck@jindraregal
```

The same from a shell:

```bash
claude plugin marketplace add jindraregal/analyse-bottleneck
claude plugin install analyse-bottleneck@jindraregal
```

With the [skills CLI](https://github.com/vercel-labs/skills), which finds `SKILL.md` in the repository root:

```bash
npx skills add jindraregal/analyse-bottleneck
```

Or as a plain skill folder:

```bash
git clone https://github.com/jindraregal/analyse-bottleneck ~/.claude/skills/analyse-bottleneck     # or any path; it is just files
python3 --version                                         # 3.8+, standard library only
python3 -m unittest discover -s ~/.claude/skills/analyse-bottleneck/tests
```

Ask Claude Code: "use analyse-bottleneck on my last 24 hours", or run the script yourself.

## Use

```bash
S=~/.claude/skills/analyse-bottleneck/scripts
python3 $S/analyze.py view --project my-app --since 7d --out ~/rb --name my-app   # my-app.view.json, my-app.html, my-app.brief.md
python3 $S/analyze.py render ~/rb/my-app.view.json --notes notes.json --out ~/rb/my-app.html   # add model notes, no re-parsing
python3 $S/analyze.py --since 24h                         # project of the current directory
python3 $S/analyze.py --session latest                    # newest session (plus its subagents)
python3 $S/analyze.py --project my-app --since 2026-10-01 --until 2026-10-15
python3 $S/analyze.py --all --since 7d --top 12           # every project on this machine
python3 $S/analyze.py --session <id-prefix> --format json --section ranked,wall_clock
python3 $S/analyze.py compare --project my-app --before "2026-10-01..2026-10-10" --after "2026-10-10.."
python3 $S/analyze.py compare --before session:abc123 --after session:def456 --project my-app
```

```bash
# with measured host telemetry (sysstat installed and collecting) and a job wrapper's records
python3 $S/analyze.py --project my-app --since 7d --sysstat --jobs ~/.local/state/my-wrapper/jobs
# delivery metrics, then fold them into the report
python3 $S/analyze.py dora --repo ~/src/my-app --since 7d --out /tmp/dora.json [--ci-runs runs.json] [--deploys deploys.jsonl] [--deploy-regex 'deploy|release']
# runs.json: a JSON list of workflow runs with the fields workflowName, conclusion, status, headBranch,
#   event, createdAt, startedAt, updatedAt (the GitHub CLI's run list with JSON output gives exactly this)
python3 $S/analyze.py --project my-app --since 7d --dora-json /tmp/dora.json
```

`--project` takes a name fragment (all matching projects) or the full encoded directory name (exactly that one).

Markdown goes to stdout; the full JSON is written to `~/.local/share/analyse-bottleneck/reports/` (`$XDG_DATA_HOME` if set) or to `--json-out PATH`. Streaming, line by line: a few hundred MB of transcripts take seconds and tens of MB of RAM. Nothing is copied.

### Example (synthetic data)

```
## Where the time went
| category | time | % of autonomous | % of span |
| human_wait:prompt | 15.0m | - | 50.6 |
| build | 8.6m | 58.7 | 29.0 |
| poll_wait | 4.0m | 27.4 | 13.5 |
| inference | 87s | 9.9 | 4.9 |

1. **Full `next build` used as a quick correctness check** - ~6.0 min, confidence medium
   - Evidence: 6 runs, 8.6m measured, 1 session
   - Arithmetic: time: 6 runs, 8.6m measured x 0.70 = 6.0m; assumption: an incremental `tsc --noEmit` plus lint answers 'does it compile' in roughly 20-35% of a production build
   - Before: `npm run build   # after every small edit`  After: `npx tsc --noEmit --incremental && npx next lint`
```

## How it works

1. **Parse** (`scripts/rbn/parse.py`): stream each `*.jsonl` and each `<session>/subagents/agent-*.jsonl`. Keep only small aggregates (tool name, class, start/end, size, error flag, a result hash). Unknown record types, missing fields and malformed lines are skipped, so version drift does not break it.
2. **Classify** (`rules/commands.json`): an ordered, data-driven table maps Bash commands to classes after removing heredocs, quoted strings and data-file names (so `grep x vitest.log` is not a test run). Other tools map by name; MCP tools by server.
3. **Attribute** (`scripts/rbn/timeline.py`): requests become thinking/inference intervals, tool calls become tool intervals (background tasks run until their completion notice), subagent files add child timelines. A priority sweep line gives every second one owner. Gaps become human waits (a human prompt follows), away time (session parked), idle (nothing happened, nobody asked) or overhead. Permission waits are inferred only when a normally instant tool took long in a non-bypass mode.
4. **Scan** (`rules/signals.json`): bounded regex scan of results of executing tools only. Output of `Read`/`Grep`, pure read commands (`cat`, `grep`), MCP and web content is never scanned, which keeps false positives down.
5. **Waste** (`scripts/rbn/quota.py`, `stack.py`): quota heuristics and the stack rules, each firing only when its evidence threshold is met. Overlapping rules share events fractionally, so savings never exceed measured time.
6. **Drill** (`scripts/rbn/drill.py`, `view.py`): the sweep also carries which tool or request won each slice; parent waits are resolved against all child transcripts of the session (nested subagents and workflow agents included, the same priority order, earliest start wins ties); chained and wrapped commands are named by their heaviest part (`rules/segments.json` lists wrappers such as `timeout N`, `nice`, `env X=Y`, `<x>-heavy ... --`). The leaves sum to the autonomous time exactly; a test checks it.
7. **Rank** (`core.py`): score = (minutes saved + units saved / units-per-autonomous-minute) x confidence weight. Everything is computed by code.
8. **Reason** (`SKILL.md`): the model reads the brief or the markdown, demotes inapplicable findings, merges shared root causes and writes the final before/after recommendations.

## Be honest about what it cannot know

- Transcripts contain **no CPU, memory or disk telemetry**. Machine signals are inferred from error text and timing, and labelled `high`, `medium` or `low`. Pass `--sysstat` (host archives, usually 10-minute resolution, host-wide so other workloads are included) or run `sample_machine.py` alongside a run and pass `--machine-log` to turn guesses into measurements. The coverage table says which applies.
- **API latency** is measured from the send record Claude Code writes before each request to the first and last content block of the reply. A record is written per complete block, so "first block" is an upper bound on time-to-first-token, not TTFT itself. Older Claude Code versions write no send records; the report then says latency is missing.
- **DORA metrics are not in transcripts.** `dora` computes lead time from git (it ends at the mainline commit, not the deploy), takes deploy frequency, change failure rate and restore time from a deploy workflow or a deploy log, and otherwise labels the git-only numbers as proxies. Incidents are never visible to it.
- Savings are **estimates with stated assumptions** (for example "incremental typecheck costs about 30% of a full build"). Edit the assumption in the rule if your project differs.
- **Units** are weighted token equivalents (cache read 0.1, cache write 1.25, output 5, times a model-tier factor from `rules/models.json`). They compare things; they are not money. Check the weights against your plan.
- Time inside a request is split into thinking, generation and an `api_wait` excess (a request far slower than its output explains: retries, overload, stalls). It is a heuristic.
- Quota findings overlap. The same turns can be polling, mechanical and batchable. Do not add them up.
- A stack rule matches **command text**. Two rules also read the project on disk, because the advice can already be in force without showing in the command: `ts-tsc-not-incremental` (a tsconfig with `incremental` or `composite`, also through `extends` including packages and through project references, or an existing `*.tsbuildinfo`) and `next-turbopack-not-used` (Next 16 or newer in `package.json`). The check uses the working directory recorded with each call, follows `cd`, `--filter` and `-p`, and reads the files as they are **now**, not as they were during the run. If the directory is gone (a deleted worktree or temp clone) the project's main checkout is checked instead; if nothing can be found, the rule reports as before. `meta.project_checks` in the JSON report lists what was left out and why.

## Rules are data

Add a file `rules/stack-mything.json`:

```json
{
  "kind": "stack_rules",
  "rules": [{
    "id": "make-clean-every-time",
    "stack": "c",
    "title": "make clean before every build",
    "detector": {"regex": "\\bmake\\s+clean\\b", "exclude_regex": "--dry-run",
                 "requires_context": "Makefile|\\bmake\\b", "requires_absent": null,
                 "followed_by": {"regex": "\\bmake\\b", "within_commands": 3}},
    "evidence": {"min_count": 3, "min_seconds": 120},
    "estimate": {"saving_fraction": 0.6, "assumption": "incremental builds are about 2.5x faster"},
    "confidence": "medium",
    "why": "Clean builds discard incremental object files.",
    "fix": "Clean only after toolchain changes.",
    "before": "make clean && make", "after": "make -j"
  }]
}
```

Detector options: `regex` / `exclude_regex` run on the sanitised command; `match_target: "result"` (+ `require_error`) matches tool output instead; `followed_by` credits the later command; `requires_context` / `requires_absent` look at everything else in the session; `skip_if_project` drops a matched call when the project on disk already does what the rule advises (see below); `type: "tool_name"` and `type: "signal_overlap"` cover non-Bash cases. Estimate options: `saving_fraction`, `seconds_per_event`, `events_minus_one`, `token_fraction`, `event_fraction`, `token_per_event`. Use `--extra-rules DIR` to keep private rules outside the repo. Other files: `commands.json` (command classes), `signals.json`, `models.json`, `advice.json`, `thresholds.json`, `segments.json` (donut slices), `describe.json` (2-5 word item names), `waiting.json` (the waiting-for-you slice and its tips), `deepdive.json` (deep dives: trigger, facts, checks on disk, tips, subagent brief). A rule's optional `source` (where the advice comes from, with a URL) is shown as "Source" under its tip.

## Watch: opt-in Stop hook for continuous observation

Purpose: deterministic, evidence-based hints after each run on where to look next, and a history you can summarise weekly, without a model doing the analysis.

**Nothing is installed for you.** The tool never edits `settings.json`. To try it:

```bash
python3 scripts/analyze.py watch-ctl print-config   # prints the hook entry to merge by hand (plugin install: print it again after an update, the path has the version in it)
python3 scripts/analyze.py watch-ctl status         # on/off, number of analyses, own cost
python3 scripts/analyze.py watch-ctl off            # kill switch (flag file); 'on' removes it
ANALYSE_BOTTLENECK_WATCH=off claude ...                 # disable for one shell
python3 scripts/analyze.py watch --report --since 7d   # periodic summary of the history
```

The printed entry:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "timeout": 10,
  "command": "python3 /path/to/analyse-bottleneck/scripts/analyze.py watch --hook-json --detach --min-interval 300"}]}]}}
```

Add it next to any Stop hooks you already have (the `Stop` list takes several entries); do not replace them. Remove the entry to uninstall.

How it behaves:

- **Non-blocking.** With `--detach` the hook reads the hook JSON, starts the analysis in a separate session at `nice 15`, and returns (about 0.1 s). Warnings from that background run are stored in `pending.json` and shown at the next stop as a `systemMessage`. It never returns a blocking decision and always exits 0, so it cannot stop or fail a session; errors go to `errors.log` in the state directory.
- **Low overhead.** It analyses only the transcript of the session that stopped (plus its subagents). It skips when the transcript has not grown, when the last run was less than `--min-interval` seconds ago, or less than 10x the last analysis time ago (so it uses at most ~10% of one core). One run at a time (lock file); a run gives up after `--max-seconds` (default 120). Typical cost: under 1 s for a normal session; a 50 MB transcript with 400 subagents takes about 50 s and 120 MB RAM in the background.
- **Incremental and periodic.** One JSON line per analysis in `$XDG_STATE_HOME/analyse-bottleneck/history.jsonl`; warnings print only when new or at least 25% worse for that session. `watch --report --since 7d` aggregates the last line of each session: time by class, recurring warnings, and the hook's own overhead.
- **Auditable.** Every history line carries `audit`: tool version, a hash of the rule files, the transcript path, size and mtime, the analysis time and memory, and the command that recomputes the same numbers.

Thresholds live in `rules/thresholds.json` (override with `--thresholds FILE`). Sessions shorter than 5 autonomous minutes are not judged.

## Job records (`--jobs`)

Any wrapper that writes one JSON file per job (or JSONL lines) works. Fields are matched by common names: start `started|start|started_at`, end `ended|end|ended_at`, queue `queue_wait_s|gate_wait_s|wait_s|lock_wait_s`, memory `peak_bytes|peak|max_rss_bytes`, exit `exit|exit_code`, `reason`, `name`, `cwd`. Jobs are matched to the analysed project by working directory (or a scratch path containing the encoded project directory). If the records were pruned and do not cover the window, the report says so.

## Machine sampler

```bash
python3 scripts/sample_machine.py --out /tmp/run.samples.jsonl --interval 5 &
# ... run your long job ...
python3 scripts/analyze.py --session latest --machine-log /tmp/run.samples.jsonl
```

The report then says things like "build ran at 96% CPU on average" or "memory available fell below 8%: OOM signals corroborated".

## Privacy

Everything runs locally. Reports can contain your commands and paths, so they go to `~/.local/share/analyse-bottleneck/reports/`, outside the skill folder and any git checkout. The home directory is shortened to `~` in printed evidence. Nothing is uploaded: the scripts make no network calls and read no credentials. CI runs and deploy records come only from files you export and pass in.

## Layout

```
SKILL.md              instructions for the model (thin reasoning layer)
scripts/analyze.py    CLI: view | render | analyze | compare | calls | dora | watch | watch-ctl
scripts/sample_machine.py
scripts/rbn/          parse, classify, timeline, drill, view, view_render, deepdive, signals, quota, stack, repo, telemetry, dora, render, compare, watch
rules/*.json          data: classes, signals, measures, stack rules, weights, advice, thresholds
CHANGELOG.md          what changed per version
tests/                python -m unittest discover -s tests   (synthetic fixtures only)
```

## Contributing

Add rules with a threshold and a stated assumption; only flag what the data can evidence. Add a test with a synthetic fixture (`tests/fixtures.py`). Keep it standard library only and streaming.

MIT licensed.
