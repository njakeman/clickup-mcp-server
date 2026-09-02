# clickup-mcp

A small MCP server for King's Digital Lab ClickUp workspace, built for
one job: updating task status and tracking time without leaving OpenCode (or any
other MCP client). Auth is a personal API token, so nothing is installed or
authorised at the ClickUp workspace level.

Single file, 24 tools. Dependencies are declared inline (PEP 723), so `uv`
resolves them per run — nothing to install.

```powershell
uv run --script clickup_mcp.py --selftest   # checks token, network, auth
```

Environment (Windows user variables; quotes around values are tolerated):
`CLICKUP_TOKEN` (required, pk_...), `CLICKUP_TEAM_ID` (26475560),
`CLICKUP_DEFAULT_LIST` (optional), `CLICKUP_CA_BUNDLE` (PEM path, for
TLS-inspecting networks), `CLICKUP_INSECURE=1` (diagnostic only).

## What you can say

No prefixes — the model picks the tool from plain English.

### Logging time (`clickup_add_time` — the everyday one)

| Say | Does |
|---|---|
| "add 20 mins to K05" | Logs 20 minutes, backdated to the period just worked |
| "log 1h30 on K10, planning session" | With a description on the entry |
| "add 45 mins to K05 yesterday" | Backdated; a bare date starts entries at 09:00 |
| "add 2 hours to Meetings in Musical Lives" | Names the project to disambiguate |
| "put 30 mins on Meetings in Slavery in War/Requirements" | For a list name that repeats across projects |

**Durations**: `20`, `20m`, `20 mins`, `1h`, `1.5h`, `1h30`, `1:30`, `2 hours 15`.
A bare number means minutes.
**Dates**: `today`, `yesterday`, `-3d` (this time of day, three days back),
`YYYY-MM-DD`. A report `end` of `today` includes the whole day.

### Status

| Say | Does |
|---|---|
| "mark K05 in progress" / "close PR01F" | Sets the status |
| "what statuses can the requirements list take?" | Per-list vocabulary; names are case sensitive on write |

### Finding things

| Say | Does |
|---|---|
| "what does K0 match in ClickUp?" | Dry run of name resolution before writing |
| "show me K05" | Full detail incl. time tracked |
| "what lists are in Sudan Memory?" / "list every ClickUp list" | Hierarchy browse |
| "what's open in the requirements list?" | Tasks with status/assignee/subtask filters |

### Docs and Project Review Records

| Say | Does |
|---|---|
| "what docs are there for Musical Lives?" | Doc search by name |
| "what happened in INCREMENT 4 of the Musical Lives PRR, any to-dos?" | Reads just that section |
| "read the Slavery in War documents doc" | Whole doc as markdown (truncates from the bottom — the newest PRR increment sits nearest the top) |

### Notes on tasks (comments)

| Say | Does |
|---|---|
| "find the task in Musical Lives most to do with IIIF, then add a note: viewer decision deferred to Sept" | Model lists the project's tasks, judges the best match, confirms, then comments |
| "add a note to K04: credentials agreed with eResearch" | Direct comment |
| "what are the latest notes on K04?" | Recent comments, newest first |
| "append to K04's description: VPN issue may be institutional" | Adds dated text to the description; existing text preserved (append-only — no replace, by design) |

### Structural changes — all two-phase

| Say | Does |
|---|---|
| "create a task in the Slavery in War requirements list: VPN blocking at UoN" | **Refused** unless the name follows the siblings' convention — suggests `K21 - VPN blocking at UoN` |
| "create K21 - VPN blocking at UoN there" | Preview (list, name, convention check) → you agree → written and read back |
| "add a subtask under K11: PR02D - ..." | `parent=` — convention checked against the parent's existing subtasks |
| "push K06 to 30 Nov" / "clear K06's due date" | Preview → confirm |
| "add Geoff to K17" / "take Geoff off K17" | Preview → confirm |
| "show K06 in detail" | Now includes custom fields (Analyst days, MoSCoW…), parent, subtasks |
| "what's open across the whole Musical Lives project?" | Project (folder) scope — every list in one call |

### Undo — by id, never "undo last"

| Say | Does |
|---|---|
| "list my time entries on K06 this week" | `time_report(detail=true)` — every entry with its id |
| "delete time entry 4392…" | Preview of exactly what would go → confirm. Refuses entries that aren't yours |
| "delete my comment 9021… on K04" | Same; comment ids are shown by `get_comments` |

Write confirmations print the id of what they created, so a mistake caught by
the read-back can be reversed immediately — or a week later, from a listing.

### Reporting, timers, housekeeping

| Say | Does |
|---|---|
| "how much time did I log last week?" | Grouped by task, with a total |
| "how much time has gone on K05?" / "time report since 2026-08-01" | Scoped variants |
| "start a timer on K05" / "stop the timer" / "is a timer running?" | Live timing; `add_time` is preferred |
| "check the ClickUp connection" | Auth, workspaces, header style in use |

## Behaviour worth knowing

- **Confirm the target first.** The instructions require the model to
  resolve a target with a read tool, show name/id/project/URL and the exact
  change, and get a yes before any write. Structural writes (create, delete,
  due date, assignee) enforce it mechanically: `confirm=false` (default) only
  previews; nothing happens until `confirm=true`.
- **Scope is a list, a project or a space.** Naming a project (folder)
  covers all its lists in one call — the main fix for slow multi-step reports.
- **Every write is verified.** The server instructions tell the model to read
  each change back from the API in the same turn and report the resulting
  state (e.g. the new tracked total) — figures from the read-back, never from
  memory. clickup_append_description does its own read-back and says so.
- **Ambiguity writes nothing.** A task name matching several tasks (or a list
  name matching several lists) refuses and lists the candidates with their
  projects. Generic names — "Meetings", "Check project status" — recur across
  most projects in this workspace, so this fires by design. Disambiguate by
  naming the project, never by loosening the refusal.
- **No default-list short-circuit.** An early draft searched the default list
  first and returned on a hit, which silently wrote "Meetings" time to the
  wrong project. Do not reintroduce it as an optimisation.
- Naming a list searches only that list, without an assignee filter (shared,
  unassigned tasks are found). Naming no list searches tasks assigned to the
  token's user across the workspace — an unassigned task therefore needs its
  list named. Every confirmation names the project it wrote to.
- Tasks resolve from a name fragment, a raw id, or a pasted task URL; docs
  likewise (name, id, or /v/dc/ URL).
- K##/PR## naming is a loose convention, not a rule — many tasks have only a
  verbose title. For "the task about Y", the model lists the project's tasks
  and judges; it should name its choice before writing to it.
- PRR increments: newest nearest the TOP (highest number first).

## Known IDs

Workspace "King's Digital Lab" `26475560` · space "Evolutionary development"
`44570316` · folder "Slavery in War" `90153190039` · list "Slavery in War -
Project Requirements List" `901512033398` · list "_Metadata" `901504991986`.

## When it breaks

1. **TLS interception** (KCL networks): `Network error ... ProxyError` or a
   certificate failure. Export the proxy CA as PEM, set `CLICKUP_CA_BUNDLE`.
   `CLICKUP_INSECURE=1` confirms the diagnosis only — never leave it set.
2. **Status names**: the UI label is not authoritative; ask for the list's
   statuses. Case sensitive on write.
3. **Auth**: v2 takes the raw token, no `Bearer`. The server tries raw,
   retries once with Bearer on 401, and `clickup_whoami` reports which style
   worked. Both failing means the token is wrong or revoked.
4. **Colleague's time comes back empty.** ClickUp may not permit your role
   to view others' entries; the tool says so when a person was named. The
   task's `tracked` figure (get_task) is the total across everyone regardless.
5. **Unexpected "nothing matched"**: usually a task not assigned to you and
   no list named.
6. **Stale hierarchy cache**: lists created mid-session appear after a client
   restart. (Docs are not cached.)
7. **Docs API shape**: the v3 pages response was verified against docs, not
   live. If `clickup_read_doc` says "no readable pages" on a real doc, dump
   the raw response and check where pages nest.
8. **429**: surfaced with the reset header, deliberately not retried.
9. **`uv` not on PATH for a GUI-launched client**: use the absolute path to
   `uv.exe` in that client's config.

## Design notes (read before extending)

- The MCP SDK renamed `FastMCP` to `MCPServer` in 2.0; the file imports
  whichever exists. Don't pin one major.
- Everything goes through `_request` (shared `httpx.Client`, auth fallback,
  429 and network diagnostics; v3 paths route on the `/v3/` prefix). Add
  endpoints there, not around it.
- `ClickUpError` carries the HTTP status; the id-probe swallows only 404/401.
- The `@tool` wrapper returns `ClickUpError` messages as tool text — the SDK
  would otherwise flatten them to "Error executing tool X". Real bugs still
  raise. `--selftest` calls the *unwrapped* functions via `__wrapped__` so
  failures exit non-zero; keep that distinction.
- Logging is pinned to stderr; the server speaks MCP on stdout, so a stray
  `print()` corrupts the protocol.
- Server-level MCP `instructions` carry the KDL conventions (Evo Dev = active
  projects; K##/PR## naming; PRR increments accumulate bottom-up) to every
  client at the handshake. Clients that ignore them can get the same text in
  CLAUDE.md / AGENTS.md.
- Two-phase writes: keep `confirm=false` as the default and the preview as
  the only possible first-call outcome. Do not add a "skip confirmation" flag.
- `_naming_convention` is a heuristic (≥60% of siblings share a `K##`-style
  prefix). Tune the threshold rather than removing the check.
- `_resolve_task`/`_resolve_list`/`_resolve_scope` refuse rather than guess, and `_search_tasks`
  never prefers one list over another. Keep those properties.
- Pagination stops at 1000 tasks / 2000 docs. Docs are read-only for now —
  writing to a PRR is a deliberate omission until reading is proven on real
  PRRs. Custom fields (the budget-table columns) are the likely next ask:
  discover field ids via `GET /api/v2/list/{list_id}/field` first.

## URLs

Single items and every write confirmation carry a full URL. Listings carry
one substitution rule in the header (`Task URL: https://app.clickup.com/t/<id>`)
rather than a URL per row — cheaper in context, and any model can substitute.
Patterns: tasks `/t/{id}`; lists `/{team}/v/li/{id}`; folders `/v/f/{id}`;
docs `/v/dc/{id}`. Saved *views* (`/v/l/...`) cannot be derived from a list
id. URLs are built by string formatting from ids already in the response —
no extra API calls.

## The `/cu` command

`cu.md` is a slash command that routes a request to the ClickUp tools. It is
deliberately short (~40 tokens): a command's body is pasted into the
transcript on every use, and the full rules already reach the model once per
session via the server's MCP `instructions` (verified: opencode and Claude
Code both place them in context). Install it in either or both clients —
same file:

- opencode: `~/.config/opencode/commands/cu.md` (project-level:
  `.opencode/commands/cu.md`)
- Claude Code: `~/.claude/commands/cu.md`

Then `/cu add 20 mins to K05`. It deliberately pins no model: a pinned model
re-reads the whole session history on that turn at full cost. If a stronger
model proves better at tool-driving, add a second command with
`model:` and `subtask: true` for self-contained requests.

The server instructions also recognise a plain `cu:` prefix, for clients
without commands.


## Colleague setup — opencode + the ClickUp server on the KCL AI platform

A step-by-step for KDL colleagues. End state: you type `/cu add 20 mins to
K05` in a terminal and it happens in ClickUp as you, with the model running
on KCL's own AI platform (arc models), so prompts stay on KCL infrastructure.
The ClickUp API itself is ClickUp's cloud, whoever drives it.

Everything here uses *your own* ClickUp token: actions appear in ClickUp as
you, with your permissions, exactly as if you had clicked them.

### 1. Get the server file

Copy `clickup_mcp.py` from [here](https://github.com/kingsdigitallab/clickup-mcp-server) into a folder of
your own, e.g. `C:\Users\<you>\Repos\clickup-mcp\`.

### 2. Install uv (runs the server, no Python setup needed)

Check with `uv --version` in PowerShell; if missing, install from
https://docs.astral.sh/uv/ . uv resolves the server's dependencies on first
run — nothing is installed into a system Python.

### 3. Generate your ClickUp token

In ClickUp: your avatar (top right) → Settings → Apps → API Token →
Generate. It starts `pk_`. Treat it like a password: it never expires and
carries all your ClickUp permissions. Don't put it in chat logs, documents,
or anything committed to git.

### 4. Set environment variables (Windows, User scope)

Either PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("CLICKUP_TOKEN", "pk_your_token", "User")
[Environment]::SetEnvironmentVariable("CLICKUP_TEAM_ID", "26475560", "User")
```

or the Environment Variables dialogue (User variables pane — no quotes
around values). Optionally add `CLICKUP_DEFAULT_LIST` with the id of the
list you use most (the number after `/li/` in its URL). Open a NEW terminal
afterwards — existing ones keep the old environment.

*(macOS/Linux: export the same variables in your shell profile.)*

### 5. Self-test before anything else

```powershell
uv run --script C:\Users\<you>\Repos\clickup-mcp\clickup_mcp.py --selftest
```

It should name you and the King's Digital Lab workspace. If it fails with a
network/TLS error on the KCL network, see "When it breaks" above (proxy CA
bundle). Don't continue until this passes.

### 6. Install and configure opencode

Install opencode (https://opencode.ai). Then edit the global config —
`~/.config/opencode/opencode.json` (create it if absent), merging in:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "kcl/arc:apex",
  "provider": {
    "kcl": {
      "name": "KCL",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://api.ai.create.kcl.ac.uk/v1",
        "apiKey": "{env:KCL_AI_API_KEY}"
      },
      "models": {
        "arc:apex": { "name": "Apex" },
        "arc:nexus": { "name": "Nexus" },
        "arc:lite": { "name": "Lite" }
      }
    }
  },
  "mcp": {
    "clickup": {
      "type": "local",
      "enabled": true,
      "command": ["uv", "run", "--script", "C:\\Users\\<you>\\Repos\\clickup-mcp\\clickup_mcp.py"]
    }
  }
}
```

Notes:
- You need a KCL AI platform key: Set it
  as a `KCL_AI_API_KEY` user environment variable (step 4 shows how); the
  `{env:...}` syntax reads it at load — do not paste the key into the file.
- `model: "kcl/arc:apex"` makes Apex the default; pick a lighter arc model
  if you prefer and use Apex per-session.
- The `mcp` block shape above matches current opencode releases; some
  versions want it nested as `mcp.servers.clickup` instead — if opencode
  complains "Configuration is invalid", try the other nesting; the error
  message names the offending key.
- JSON needs doubled backslashes in Windows paths.

### 7. Install the /cu command

Copy `cu.md` into `~/.config/opencode/commands/` (create the folder if
needed). This routes `/cu <request>` to the ClickUp tools explicitly, so you
can be terse without the model wandering off to answer from files.

### 8. Smoke test, in order

Start `opencode` in any folder and run these one at a time:

1. `/cu check the clickup connection` — should name you and the workspace.
2. `/cu what does K05 match?` — read-only name resolution.
3. `/cu add 5 mins to <a task of yours>, description "setup test"` — first
   real write. Verify the entry in the ClickUp UI, not just the reply.
4. `/cu list my time entries today in detail` then
   `/cu delete time entry <id>` — confirms undo works; it previews first and
   asks before deleting. Your test entry is now cleaned up.

### Expectations worth knowing

- Ambiguous names are refused, with candidates listed — say which you meant
  (or name the project: "Meetings in Musical Lives"). This is deliberate.
- Structural changes (create task, delete, due date, assignee) always show a
  preview and wait for your yes.
- Everything you write is attributed to you in ClickUp, immediately, exactly
  like the UI. The server adds no rate of its own - be as careful as you
  would be clicking.
- Time reports for colleagues (`person=`) work only if your ClickUp role may
  view their entries.

## Client registration

- **Claude Code**: `claude mcp add clickup -- uv run --script <path>\clickup_mcp.py`
- **Claude desktop app**: see `claude_desktop_config.json` snippet below.
- **opencode** (this version's flat schema): in `~/.config/opencode/opencode.jsonc`:
  `"mcp": {"clickup": {"type": "local", "enabled": true, "command": [...]}}`

```json
{
  "mcpServers": {
    "clickup": {
      "command": "uv",
      "args": ["run", "--script", "C:\\Users\\neil_\\Repos\\clickup-mcp\\clickup_mcp.py"]
    }
  }
}
```
