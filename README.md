# analyse-bottleneck

**analyse-bottleneck is a free, open-source Claude Code plugin that reads your local Claude Code transcripts and shows where the time and quota of your agent runs went (tests, builds, deploys, polling, model time, waiting for you), with ranked fixes.**

For developers who run long or autonomous Claude Code sessions and want the next run to be faster and cheaper. MIT licensed, runs locally, made by Jindra Regál. Project page: [jindraregal.com/analyse-bottleneck](https://jindraregal.com/analyse-bottleneck).

## Install

```bash
npx skills add jindraregal/analyse-bottleneck            # this project
npx skills add jindraregal/analyse-bottleneck --global   # or every project
```

Or as a plugin:

```
/plugin marketplace add jindraregal/analyse-bottleneck          # inside Claude Code
/plugin install analyse-bottleneck@jindraregal
claude plugin marketplace add jindraregal/analyse-bottleneck    # or the same from a shell
claude plugin install analyse-bottleneck@jindraregal
```

Then ask Claude Code: "use analyse-bottleneck on my last 24 hours" (or "on my last 7 days", "on this session"). Needs Python 3.8+, standard library only.

## How it works

It reads the transcripts Claude Code already writes to `~/.claude/projects`, streaming (a few hundred MB take seconds). About 90% of the work is plain Python: every second of wall-clock time goes to exactly one cause, parallel tools and subagents are never double counted, and waits on subagents and background tasks are resolved to what they were doing. The model only reads a compact brief (<= 60 lines) and adds judgement, so it is cheap on a tight weekly quota.

## What you get

- **One HTML page** (`view`): a donut of agent time by cause plus a "Waiting for you" slice (your next message, permission prompts, approval gates), with ranked "Do this" fixes and the exact commands behind each slice.
- **Ranked fixes** with evidence, before/after commands and estimated savings with the arithmetic shown.
- **Environment signals** from tool output: disk full, OOM, timeouts, rate limits (429), overload (529), retry loops, each with a confidence label.
- **Quota waste**: cache hit rate and rebuilds, oversized tool outputs, repeated reads and commands, sleep polling, top-tier model on mechanical turns.
- **Stack rules** as data in `rules/*.json`: Next.js, npm vs pnpm/bun, pip vs uv, jest vs vitest, tsc incremental, Docker cache, Playwright reruns, ffmpeg, whisper and more. Add your own with `--extra-rules DIR`.
- **Deep dives** when one cause dominates (today: deploys), with checks of the project's files on disk.
- **Other commands**: `calls` (every tool call and model turn), `compare` (two windows or sessions, to prove a fix helped), `dora` (delivery metrics from git and exported CI runs), `watch` (opt-in, non-blocking Stop hook; never installed for you).
- **A coverage matrix** saying which numbers are measured, inferred or missing. Optional `--sysstat`, `--jobs` and `scripts/sample_machine.py` turn inferred machine signals into measurements.

## Example (synthetic data)

```
## Where the time went
| category | time | % of autonomous | % of span |
| human_wait:prompt | 15.0m | - | 50.6 |
| build | 8.6m | 58.7 | 29.0 |
| poll_wait | 4.0m | 27.4 | 13.5 |

1. **Full `next build` used as a quick correctness check** - ~6.0 min, confidence medium
   - Arithmetic: 6 runs, 8.6m measured x 0.70 = 6.0m
   - Before: `npm run build   # after every small edit`  After: `npx tsc --noEmit --incremental && npx next lint`
```

## Run it yourself

```bash
S=~/.claude/skills/analyse-bottleneck/scripts
python3 $S/analyze.py view --project my-app --since 7d --out ~/rb --name my-app   # .html, .brief.md, .view.json
python3 $S/analyze.py compare --project my-app --before "2026-10-01..2026-10-10" --after "2026-10-10.."
```

Full options and the model's flow are in [SKILL.md](SKILL.md); changes per version in [CHANGELOG.md](CHANGELOG.md).

## Limits and privacy

Transcripts hold no CPU, memory or disk telemetry, so machine signals are inferred unless you pass measurements. Savings are estimates with stated assumptions you can edit in the rules. Everything runs locally: no network calls, no credentials read, nothing uploaded. Reports go to `~/.local/share/analyse-bottleneck/reports/`, outside any git checkout.

## FAQ

### What is analyse-bottleneck?

A Claude Code plugin (also installable as a plain Agent Skill) that answers "where did my agent's time go?". It reads the session transcripts Claude Code keeps in `~/.claude/projects`, puts every second of a run into one cause (tests, builds, deploys, polling, model time, waiting for you, ...) and ranks fixes by the time they would save.

### How do I install analyse-bottleneck in Claude Code?

From a shell:

```bash
claude plugin marketplace add jindraregal/analyse-bottleneck
claude plugin install analyse-bottleneck@jindraregal
```

Inside Claude Code the same works with `/plugin` instead of `claude plugin`. Without the plugin system: `npx skills add jindraregal/analyse-bottleneck`.

### How do I run it?

Ask Claude Code in plain words, for example "use analyse-bottleneck on my last 7 days" or "why was my last session so slow?". It writes an HTML report and answers with the 2-3 causes that matter and the one thing to change first. You can also run `scripts/analyze.py view` yourself (see "Run it yourself").

### Why is my Claude Code agent so slow?

Often the model is not the main cost. The usual suspects are full builds used as a quick check, whole test suites rerun after small edits, deploys that build things nobody uses, `sleep` loops polling CI, and time spent waiting for your reply or a permission prompt. In the author's own 43 hours of runs the model took 16% of the time ([write-up](https://jindraregal.com/blog/why-ai-coding-agents-are-slow)). analyse-bottleneck measures which of these applies to your runs.

### Does it upload my transcripts or code anywhere?

No. The scripts make no network calls, read no credentials and write reports only to your machine (`~/.local/share/analyse-bottleneck/reports/` by default). Claude reads a short brief in your own session.

### How much quota does it use?

Little. Parsing and the arithmetic are plain Python; the model reads a brief of at most 60 lines. Only when one cause dominates (for example deploys) does it start one read-only subagent for a deep dive.

### Does it work with Codex, Cursor or other agents?

No. It reads the transcript format of Claude Code only.

### How do I check that a fix helped?

Ask Claude Code to compare the runs before and after the change, or run `analyze.py compare --project my-app --before "2026-10-01..2026-10-10" --after "2026-10-10.."` (see "Run it yourself").

### Who made it?

Jindra Regál. It started as a way to find out why the author's own agent runs got slower. Background: [My AI coding agents got slower. Turns out the mess was mine](https://jindraregal.com/blog/why-ai-coding-agents-are-slow).

## License

MIT. Short machine-readable summary: [llms.txt](llms.txt). Tests: `python3 -m unittest discover -s tests` (synthetic fixtures only).
