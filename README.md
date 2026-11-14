# analyse-bottleneck

A Claude Code skill that shows where the wall-clock time and quota of your agent runs went (tests, builds, deploys, polling, model time, waiting for you) and ranks the fixes.

For anyone running long or autonomous Claude Code sessions who wants the next run to be faster and cheaper. Free, MIT licensed, runs locally.

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

Then ask Claude Code: "use analyse-bottleneck on my last 24 hours". Needs Python 3.8+, standard library only.

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

Background: [My AI coding agents got slower. Turns out the mess was mine](https://jindraregal.com/blog/why-ai-coding-agents-are-slow).

## License

MIT. Tests: `python3 -m unittest discover -s tests` (synthetic fixtures only).
