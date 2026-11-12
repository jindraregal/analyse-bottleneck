---
name: analyse-bottleneck
description: Find where the time and quota of long-running autonomous Claude Code runs really went, and how to speed them up. Use when a run, session or set of sessions felt slow or burned too much quota, when asked to "analyse my runs", "why was this so slow/expensive", "what is the bottleneck", to compare before/after an optimisation, or to set up repeated observation. Static analysis of ~/.claude transcripts by a script; the model only reads the compact result.
---

# analyse-bottleneck

A script does the analysis; you read its compact output and add judgement. You never read raw transcripts.

## Default flow: one chart

`${CLAUDE_SKILL_DIR}` is the directory that contains this file (Claude Code fills it in; if you see it unfilled, use that directory). `OUT` is where the user wants reports (default `~/.local/share/analyse-bottleneck/reports/`).

1. Run the view (same scope options as below: `--project`, `--all`, `--session`, `--since`, `--until`, `--sysstat`, `--jobs`, `--dora-json`):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py view --project my-app --since 7d --out OUT --name my-app
   ```
   It writes `OUT/my-app.view.json` (the data), `OUT/my-app.html`, `OUT/my-app.brief.md` (<= 60 lines) and, when a deep dive triggers, `OUT/my-app.deepdive-<id>.md`.
   The page is fully built by the script, the same way every time: the title, one plain sentence, four small stats with icons (possible saving, recommendations, period, sessions) and "How to read this"; a donut of agent time plus a hatched "Waiting for you" slice, with agent time and the waiting in its centre. It opens on "All slices" ("Do this first" across slices, then any deep dive); picking a slice shows its "Do this" (coded rules, AI notes) and "What exactly" (items with 2-5 word names from `rules/describe.json` or the agent's own call descriptions); picking it again, the centre, "Show all" or Esc goes back to All. Every list is the same expandable row, and each panel has "Expand all". Do not write HTML, headlines, summaries or item names yourself.
2. Read only `my-app.brief.md`. Each slice lists numbered items (`tests.1`, `poll.2` ...: the command, model or tool, its hours, runs, p50, who ran it, `[shown as: name]`), a `by task:` line where the items explain less than 70% of the slice, and coded actions with their savings. `## Waiting for you [waiting]` lists how the sessions waited on the person (`waiting.1` ...) and its coded tips.
3. Optional, and the only thing you add: `notes.json` with at most 2 short notes per slice where you know something the rules cannot (a repo file that changes the advice, a rule that does not apply). Data only, no prose elsewhere:
   ```json
   {"model": "<your model id>",
    "segments": {"tests": [{"title": "<= 70 chars, imperative", "detail": "<= 300 chars", "evidence": ["tests.1: 6.1 h, 41 runs"]}],
                 "waiting": [{"title": "...", "detail": "...", "evidence": ["waiting.1: ..."]}]}}
   ```
   Then re-render without re-parsing: `python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py render OUT/my-app.view.json --notes notes.json --out OUT/my-app.html`.
   Notes appear under "Do this" of their slice with an AI badge. Cite item ids and numbers from the brief; do not restate numbers you cannot point to. If a slice is mostly "Waiting — cause unknown", say so instead of guessing. Skip notes that only repeat a rule. A `headline` field is ignored.
4. Deep dive (only when the brief has a `## Deep dive: ... [deepdive:<id>]` section, e.g. deploys took >= 10% of agent time): start ONE subagent whose prompt is the file `OUT/my-app.deepdive-<id>.md`, read-only. It works from the brief's facts and the repo's files. Never hand it tokens, keys or logins found on this machine. If the platform's own deploy records would help (build history, deploy log), ask the user to export them to a file and give the subagent that path. It returns `{"model": ..., "segments": {"deepdive:<id>": [...]}}`. Check each note against the brief's facts (or have a second subagent verify them), drop what does not hold, merge the rest into `notes.json` under the same key and re-render. Until then the page says "AI deep dive not run yet". The facts, the checks of the project's files and the coded tips are already on the page.
5. Answer in a few lines: the 2-3 slices that matter, the one thing to change first, and the HTML path.

## Token discipline (hard rules)

- Never `cat`, `Read`, `grep -r` or `head` a `.jsonl` transcript. They are huge and the script already digests them.
- Read the brief (or the markdown of `analyze`), nothing bigger. For more detail use `--format json --section <names>` one section at a time. Sections: `wall_clock`, `time_sinks`, `top_commands_by_class`, `environment`, `quota`, `stack_findings`, `ranked`, `recurring`, `sessions`, `telemetry`.
- Do not copy transcripts or paste output back into files; keep the final answer short.

## Other commands

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py --since 24h                    # full text report, project of the current directory
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py --session latest
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py --project my-app --since 2026-10-01 --until 2026-10-15
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py calls --project my-app          # every call: duration and what it did (HTML + CSV)
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py dora --repo PATH --since 7d --out FILE   # + --ci-runs FILE when the user exported CI runs
python3 ${CLAUDE_SKILL_DIR}/scripts/analyze.py compare --project my-app --before "2026-10-01..2026-10-10" --after "2026-10-10.."
```

A full encoded project directory (`--project -home-me-git-app`) works as is. If the script finds no sessions, check the fragment with `--all --since 7d` before asking the user. If the user names a private report path, write only there (`--out`, `--json-out`).

## Interpret

1. **Time shares.** In the brief and the text report, percentages are of autonomous time (human and permission waits and idle gaps are separated and excluded). On the page the ring is agent time plus the hatched waiting-for-you slice (idle gaps stay out); the overview shows each slice's time and what it can save, not its share. In the view, waits on subagents and background tasks are already resolved into what the child or the background command was doing; what cannot be resolved is the slice "Waiting — cause unknown". In the text report, `subagent_wait` and `background_wait:*` are parent waits; the line "inside subagent/background waits" says what the children did.
2. **Ranked list.** Already sorted by coded score (minutes saved + token-equivalents, weighted by confidence). Each item carries evidence, arithmetic, why, fix, before, after. Trust the arithmetic, challenge the assumption.
3. **Measured vs inferred.** The "Coverage" table says, per dimension, whether numbers are measured, inferred, a snapshot or missing. Keep that distinction in your answer: label inferred items as hypotheses and name missing telemetry instead of filling the gap. Environment signals are inferred from tool output; quote their confidence. If `/var/log/sysstat` exists, rerun with `--sysstat`; otherwise suggest `scripts/sample_machine.py` and `--machine-log`.
4. **Quota.** Units are weighted token equivalents (cache read cheap, cache write dear, top-tier model dearer), a comparison device, not money. A cache miss, an oversized output carried for 100 turns or a polling loop on the top model is usually bigger than any single slow command.
5. **Recurring** items (same finding in 2+ sessions) are the real habits; lead with them.

## Add judgement (thin layer, not re-analysis)

- Drop or demote findings that cannot apply (a hardware-encode tip on a host without a GPU, Turbopack advice on a Next version where it is already the default, a test-suite tip when the suite is 5 seconds).
- Merge findings with one root cause (a cold cache and repeated full builds are one story).
- Prefer fixes that remove a whole class of waste (a standing rule in the project instructions, an allow-rule, a script) over one-off tips.
- If a rule fired on little evidence, say so; do not inflate.
- Do not invent numbers. Every saving you state must trace to an arithmetic line in the output.

## Output format (text report, when the user asks for the full analysis)

Verdict (1-2 sentences: autonomous time, what dominated, quota headline); ranked bottlenecks (title, saving, confidence, evidence, before/after, why); recurring habits with a standing fix; inferred environment signals with confidence; one next step (and whether to re-run `compare`). Top 3-6 items.

## Repeated observation (optional, offer, never install on your own)

`scripts/analyze.py watch --detach` is an opt-in Stop hook: it returns at once, analyses the stopped session in the background, and shows warnings at the next stop. `watch-ctl print-config|status|on|off` prints the hook entry and switches it; `watch --report --since 7d` summarises the history. Do not edit the user's settings unless they explicitly ask, and then add the entry next to existing Stop hooks.

## Extending

Rules are data in `${CLAUDE_SKILL_DIR}/rules/*.json`: command classes (`commands.json`), environment signals (`signals.json`), stack-waste rules (`stack-*.json`), model weights (`models.json`), advice text (`advice.json`), watch thresholds (`thresholds.json`), donut slices, command wrappers and finding-to-slice mapping (`segments.json`), item names (`describe.json`), the waiting-for-you slice and its tips (`waiting.json`), deep dives with their trigger, checks on disk, tips and subagent brief (`deepdive.json`). A rule's `source` (docs URL) shows as "Source" under its tip; adopt outside advice only with a source that says it. A badly named item is fixed with a rule in `describe.json`, never by hand in the page. Add a rule by adding an object; see the README for the schema. `python3 -m unittest discover -s ${CLAUDE_SKILL_DIR}/tests` must stay green.
