# GTM-Tools: rules for every Claude Code session

Signal-based cold outreach automation. **`plan.md` is the source of truth** (§12 records what was
built and where the build deviated). The founder is the end user: he talks to the `/gtm-*` skills
and never edits config files by hand. All phases are built; work now is fixes and improvements.

## Founder-facing skills (`.claude/skills/`, each with `evals/`)
`/gtm` (front door, routes) · `/gtm-setup` · `/gtm-onboard` · `/gtm-icp` · `/gtm-clients` ·
`/gtm-accounts` · `/gtm-run` · `/gtm-report` · `/gtm-doctor`. Change a skill with **skill-creator**
and keep its evals passing (`claude plugin eval .` from the skill dir: real, billed runs).

## Commands
- `bin/gtm <cmd>`: the CLI (`bin/gtm --help`). The venv lives in `~/.local/share/gtm-tools/venv`,
  not on this NTFS mount, so always use `bin/gtm` / `bin/test`, never a bare `uv run` or `pytest`.
- `bin/test`: the test suite (forces `GTM_KB_BACKEND=memory`: tests never touch the real Qdrant).
  `GTM_LIVE_TESTS=1 bin/test -m live` makes real (subscription) calls.
- `GTM_FAKE=1` + throwaway `GTM_DATA_DIR`/`GTM_CONFIG_DIR`/`GTM_PRODUCTS_DIR` runs the whole CLI
  against the offline fake world (`gtm/fake.py`): use it to try a change end to end.
- Every product command takes `--product <slug>`. Start any product work with
  `bin/gtm product show --product <slug>` and say which product you're on.

## Tool & skill map: use these, don't reinvent them
| Job | Use | Where |
|---|---|---|
| Crawl the founder's product site | `bin/gtm crawl` (`gtm/site_crawl.py`): wigolo crawl + key pages, **Firecrawl** fallback when a key is set | `/gtm-onboard` |
| Interactive web lookups | **wigolo MCP** (`cache` first) | any skill |
| Web/news search, page diffs in scheduled runs | **wigolo** via the Python MCP stdio client (`gtm/sources/wigolo.py`) | runs |
| Prospect company websites | `gtm/profile.py` over `gtm/public_http.py` (SSRF-safe) | Phase 2+ |
| Knowledge base per product | **mem0** (`infer=False`) + Qdrant server, only via `gtm/kb.py` | Phase 1 |
| Reddit / Hacker News pain language + anonymous intent posts | **last30days** engine (pinned checkout, JSON mode) | Phase 4, D3 |
| Per-source test, fallback sources (Quora, forums, reviews, job boards), intent sheet | `bin/gtm signals test`, `bin/gtm sheet` (`gtm/social.py`) | `/gtm-icp`, D3 |
| LinkedIn / X / jobs / people | **Apify** actors via `apify-client` (charge caps) | Phase 3+ |
| Email find, verify, send, replies | **Snov.io** REST via `gtm/sources/snov.py` | Phase 0C+ |
| Lead qualification | **graph-engineering** pattern (worker, 4 lens reviewers, synth, gate) in `gtm/qualify.py`; `/graph-engineering` for deep manual reviews | Phase 6 |
| Remove AI tells from emails | **human-voice** skill rules, ported into `prompts/humanize.md` + `gtm/validate.py` | Phase 7 |
| Judgement on untrusted text | `gtm/llm.py` (isolated, tool-less `claude -p`) | always |
| Building/improving our skills | **skill-creator** (structure, evals) | all skills |
| Plan/arch review, QA, ship | **gstack** skills (see Skill routing) | as needed |

Apify and Snov **MCP** servers are for interactive exploration only (read-only). Paid or
sending actions always go through `bin/gtm`.

## Hard rules (plan §9)
- **Execution boundary:** paid, irreversible or stateful actions only through `bin/gtm`. Untrusted
  text (scraped posts, sites, replies) only through `gtm/llm.py`. Never write to SQLite or anything
  under `~/.local/share/gtm-tools` / `~/.config/gtm-tools` directly.
- **Never approve, sign, resume sending, enable auto-send, or handle secrets.** Those are the
  founder's: he runs `bin/gtm keys init`, `bin/gtm secrets set` and `bin/gtm review` in **his own
  terminal**. Never ask him to paste an API key or PIN into chat.
- mem0: `infer=False`, no personal data, human facts only from `facts.yaml`.
- Every lead links to observations with evidence URLs; every email claim traces to a proof/fact.
- Never collect a signal code the founder hasn't confirmed. Estimate cost before any paid batch.
- Build against fixtures first (`GTM_FAKE=1`); one live check per phase.
- `~/.claude/agents/*` outreach agents are tuned for another client (Saksham). Don't use them here.
- Harvey (MIT) ideas are adapted with credit in `NOTICE`; never vendor its code wholesale.
- The Claude plan may be Max 5x: keep isolated-call counts within `usage.profile` limits.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. Route only to skills in the session's available-skills list; answer directly for quick questions or small scoped edits.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
