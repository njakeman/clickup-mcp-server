# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2", "httpx>=0.27"]
# ///
"""
clickup_mcp.py - a small MCP server for ClickUp, built for one job:
updating task status and tracking time without leaving Claude.

Auth is a personal API token (pk_...), read from the environment. The token
never appears in tool arguments or output, so it cannot leak into a transcript.

Environment variables
---------------------
CLICKUP_TOKEN         (required)  personal API token, starts with pk_
CLICKUP_TEAM_ID       (optional)  default workspace id; avoids passing it every call
CLICKUP_DEFAULT_LIST  (optional)  default list id for task lookups
CLICKUP_CA_BUNDLE     (optional)  path to a PEM bundle, for networks that
                                  intercept TLS (see the handoff brief)
CLICKUP_INSECURE      (optional)  set to 1 to disable TLS verification.
                                  Diagnostic use only - never leave this on.
"""

from __future__ import annotations

import os
import re
import logging
import sys
import time
import datetime as dt
import functools
from typing import Any

import httpx

# The MCP Python SDK renamed FastMCP to MCPServer in 2.0. The decorators and
# handler signatures are identical, so support whichever is installed rather
# than pinning to one major and going stale.
try:  # SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

# This server speaks MCP over stdin/stdout, so anything printed to stdout
# corrupts the protocol. Pin logging to stderr and quiet httpx's per-request
# chatter.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API = "https://api.clickup.com/api/v2"
API_ROOT = "https://api.clickup.com/api"  # for v3 paths, e.g. /v3/workspaces/...

def _env(*names: str) -> str:
    """Read an environment variable, forgiving how it was set.

    Windows' Environment Variables dialogue stores the value literally, so a
    token pasted as 'pk_...' or "pk_..." keeps its quotes - unlike PowerShell,
    which strips them. Strip whitespace and one layer of quotes so both routes
    behave the same.
    """
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        if v:
            return v
    return ""


TOKEN = _env("CLICKUP_TOKEN", "CLICKUP_API_TOKEN", "CLICKUP_API_KEY")
DEFAULT_TEAM = _env("CLICKUP_TEAM_ID")
DEFAULT_LIST = _env("CLICKUP_DEFAULT_LIST")

_ca = _env("CLICKUP_CA_BUNDLE")
if os.environ.get("CLICKUP_INSECURE") == "1":
    VERIFY: Any = False
elif _ca:
    VERIFY = _ca
else:
    VERIFY = True

# ClickUp v2 expects the raw token in the Authorization header, with no
# "Bearer " prefix. Some proxies rewrite that, so we fall back once and
# remember which form worked.
_auth_style = "raw"

_client: httpx.Client | None = None


def _http() -> httpx.Client:
    """One shared client for the life of the process.

    Reusing the connection avoids a fresh TCP+TLS handshake per call, which
    is a real cost through a TLS-inspecting proxy like KCL's. httpx.Client
    is thread-safe for requests.
    """
    global _client
    if _client is None:
        _client = httpx.Client(timeout=30.0, verify=VERIFY)
    return _client

_INSTRUCTIONS = """\
ClickUp server for Neil's King's Digital Lab (KDL) workspace. Conventions:

- Active projects live in the "Evolutionary development" space (say "Evo Dev").
  Time is almost always logged there. "Feasibility" holds pre-project work;
  "Early concept" holds high-level enquiries. When a task or list name is
  ambiguous across spaces, Evo Dev is the likeliest intent - but never guess:
  the tools refuse ambiguous writes and list candidates; relay those.
- Some tasks follow a K## (milestone) or PR## (project requirement) naming
  convention and those fragments resolve directly - but this is loose, not a
  rule. Plenty of tasks have only a verbose title ("Review data dashboard
  application with centre leads"). For a vague request like "the task about
  Y in project X", list the project's tasks with clickup_list_tasks and judge
  which title best matches the activity; name your choice to Neil before
  writing anything to it.
- Each Evo Dev project has a Documents area (meetings, emails) including a
  Project Review Record ("PRR"). PRRs contain embedded tables, and narrative
  sections labelled INCREMENT 1, INCREMENT 2, ... The NEWEST increment sits
  nearest the TOP of the document (highest number first). Use clickup_read_doc
  with section="INCREMENT n" for one increment; look for to-do or action
  items within that section's text.
- Notes on tasks are ClickUp comments: clickup_add_comment writes one,
  clickup_get_comments reads recent ones.
- clickup_time_report takes person="Geoffroy" to report a colleague's time
  (permissions permitting); clickup_get_task's "tracked" figure is the total
  across everyone. Neither is limited to Neil's own time.
- Prefer clickup_add_time over timers. Durations like "20m"/"1h30"; a bare
  date starts entries at 09:00.
- URLs: the tools print a task/list/doc URL on single items and write
  confirmations, and a substitution rule (e.g. "Task URL: .../t/<id>") on
  listings - form links from those. Never invent a URL.
- A message beginning "cu:" or "/cu" is a ClickUp request: use these tools
  for it rather than answering from memory or from files.
- CONFIRM THE TARGET BEFORE ANY WRITE: resolve it with a read tool first
  (clickup_find_task / clickup_get_task / clickup_list_lists), show Neil the
  task or list NAME, id, project and URL together with exactly what will
  change, and wait for a yes. Structural writes (create, delete, due date,
  assignee) enforce this mechanically: the first call only previews; pass
  confirm=true only after Neil has agreed to that preview.
- Undo is id-based, never "undo last": clickup_time_report(detail=true) and
  clickup_get_comments show ids; the delete tools preview, refuse anything
  not Neil's own, and act only with confirm=true.
- VERIFY EVERY WRITE: after any change (status, time, comment, description),
  read the changed thing back from the API in the same turn - clickup_get_task
  for status/time/description, clickup_get_comments for notes - and report the
  resulting state to Neil (e.g. the task's new tracked total), so an error is
  spotted immediately rather than lost in history. Report figures from the
  read-back, never from memory of what was sent.
"""

try:
    mcp = _Server("clickup", instructions=_INSTRUCTIONS)
except TypeError:  # an SDK vintage without the instructions parameter
    mcp = _Server("clickup")


def tool(fn):
    """Register a tool, returning our own errors as readable text.

    The SDK turns an uncaught exception into a bare "Error executing tool
    <name>", which would throw away everything useful: the list of candidate
    tasks behind an ambiguity refusal, the TLS hint behind a network failure,
    the examples behind a bad duration. Those messages are the whole point, so
    ClickUpError is caught and returned as the tool's result instead.
    Unexpected exceptions still propagate - those are bugs, not guidance.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ClickUpError as e:
            return str(e)

    return mcp.tool()(wrapper)


class ClickUpError(RuntimeError):
    """An error with a message meant for the person, not a traceback.

    status carries the HTTP status code when the error came from an API
    response, so callers can distinguish "does not exist" (404) from
    "something is wrong" (auth, rate limit, network) - the ID-probe in
    _resolve_task must swallow only the former.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _headers() -> dict[str, str]:
    tok = TOKEN if _auth_style == "raw" else f"Bearer {TOKEN}"
    return {"Authorization": tok, "Content-Type": "application/json"}


def _request(method: str, path: str, **kw: Any) -> Any:
    global _auth_style
    if not TOKEN:
        raise ClickUpError(
            "No token found. Set CLICKUP_TOKEN in the MCP server's env block "
            "(avatar -> Settings -> Apps -> API Token in ClickUp)."
        )
    url = f"{API_ROOT}{path}" if path.startswith("/v3/") else f"{API}{path}"
    for attempt in range(2):
        try:
            r = _http().request(method, url, headers=_headers(), **kw)
        except httpx.TransportError as e:
            # Covers connect failures, proxy refusals, timeouts and TLS
            # verification errors - all the ways a corporate network says no.
            hint = (
                "If this machine sits behind a TLS-inspecting proxy, export the "
                "proxy's CA certificate and point CLICKUP_CA_BUNDLE at the PEM "
                "file. If the proxy needs an explicit route, set HTTPS_PROXY."
            )
            raise ClickUpError(
                f"Network error reaching {url}: {type(e).__name__}: {e}\n{hint}"
            ) from e

        if r.status_code == 401 and attempt == 0:
            _auth_style = "bearer" if _auth_style == "raw" else "raw"
            continue
        if r.status_code == 429:
            raise ClickUpError(
                "Rate limited by ClickUp (429). Wait a minute and retry; "
                f"reset hint: {r.headers.get('X-RateLimit-Reset', 'unknown')}"
            )
        if r.status_code >= 400:
            raise ClickUpError(
                f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}",
                status=r.status_code,
            )
        if not r.content:
            return {}
        return r.json()
    raise ClickUpError(
        "Authentication failed with both raw and Bearer header styles. "
        "Check the token is current and starts with pk_."
    )


def _team(team_id: str | None) -> str:
    tid = (team_id or DEFAULT_TEAM).strip()
    if not tid:
        raise ClickUpError(
            "No workspace id. Pass team_id, or set CLICKUP_TEAM_ID in the "
            "server env. It is the number after app.clickup.com/ in the URL."
        )
    return tid


def _list(list_id: str | None) -> str:
    lid = (list_id or DEFAULT_LIST).strip()
    if not lid:
        raise ClickUpError(
            "No list id. Pass list_id, or set CLICKUP_DEFAULT_LIST in the "
            "server env. It is the number after /li/ in a list URL."
        )
    return lid


def _ms(value: str | None, *, default_days_back: int | None = None) -> int | None:
    """Accept 'YYYY-MM-DD', 'today', 'yesterday', '-7d', or unix ms."""
    if value is None:
        if default_days_back is None:
            return None
        d = dt.datetime.now() - dt.timedelta(days=default_days_back)
        return int(d.timestamp() * 1000)
    v = value.strip().lower()
    if v.isdigit() and len(v) >= 12:
        return int(v)
    now = dt.datetime.now()
    if v == "today":
        d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif v == "yesterday":
        d = (now - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif re.fullmatch(r"-\d+d", v):
        d = now - dt.timedelta(days=int(v[1:-1]))
    else:
        try:
            d = dt.datetime.strptime(v, "%Y-%m-%d")
        except ValueError as e:
            raise ClickUpError(
                f"Could not read '{value}' as a date. Use YYYY-MM-DD, today, "
                f"yesterday, or -7d."
            ) from e
    return int(d.timestamp() * 1000)


def _fmt_ms(ms: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def _dur(ms: Any) -> str:
    try:
        total = int(ms)
    except Exception:
        return "-"
    sign = "-" if total < 0 else ""
    total = abs(total) // 1000
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{sign}{h}h {m:02d}m"


def _minutes(value: str | int) -> int:
    """Read a duration the way a person writes one.

    Accepts: 20, "20", "20m", "20 mins", "20 minutes", "1h", "1.5h", "1h30",
    "1h 30m", "2 hours 15", "0.25h". Returns whole minutes.
    """
    if isinstance(value, (int, float)):
        mins = int(round(float(value)))
        if mins <= 0:
            raise ClickUpError("Duration must be more than zero minutes.")
        return mins

    v = str(value).strip().lower().replace(",", ".")
    if not v:
        raise ClickUpError("No duration given.")

    # Bare number means minutes: "20"
    if re.fullmatch(r"\d+(\.\d+)?", v):
        mins = int(round(float(v)))
        if mins <= 0:
            raise ClickUpError("Duration must be more than zero minutes.")
        return mins

    # "1h30" / "1:30" - hours then bare minutes
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|:)\s*(\d+)?\s*(?:m|min|mins|minute|minutes)?", v)
    if m:
        total = float(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)
        mins = int(round(total))
        if mins <= 0:
            raise ClickUpError("Duration must be more than zero minutes.")
        return mins

    # General: sum every "<number><unit>" pair found, e.g. "2 hours 15 mins"
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)", v):
        found = True
        total += float(num) * (60 if unit.startswith("h") else 1)
    if not found:
        raise ClickUpError(
            f"Could not read '{value}' as a duration. Try '20m', '1h30', "
            f"'1.5h' or a bare number of minutes."
        )
    mins = int(round(total))
    if mins <= 0:
        raise ClickUpError("Duration must be more than zero minutes.")
    return mins


_ID_RE = re.compile(r"^[0-9a-z]{6,12}$")


_LIST_CACHE: list[dict[str, str]] | None = None


def _all_lists(team_id: str | None = None) -> list[dict[str, str]]:
    """Every list in the workspace, with the space and folder it sits in.

    Walked once per process and cached - the hierarchy rarely changes inside a
    session, and re-walking it on every lookup would be wasteful.
    """
    global _LIST_CACHE
    if _LIST_CACHE is not None:
        return _LIST_CACHE

    out: list[dict[str, str]] = []
    spaces = _request("GET", f"/team/{_team(team_id)}/space").get("spaces") or []
    for sp in spaces:
        sid, sname = str(sp.get("id")), sp.get("name") or ""

        # Lists sitting directly in the space, outside any folder
        for l in _request("GET", f"/space/{sid}/list").get("lists") or []:
            out.append({"id": str(l.get("id")), "name": l.get("name") or "",
                        "folder": "", "space": sname})

        # Lists inside folders. Some responses nest them, some do not.
        for f in _request("GET", f"/space/{sid}/folder").get("folders") or []:
            fid, fname = str(f.get("id")), f.get("name") or ""
            lists = f.get("lists")
            if lists is None:
                lists = _request("GET", f"/folder/{fid}/list").get("lists") or []
            for l in lists:
                out.append({"id": str(l.get("id")), "name": l.get("name") or "",
                            "folder": fname, "space": sname})

    _LIST_CACHE = out
    return out


def _resolve_list(ref: str | None, team_id: str | None = None) -> str | None:
    """Turn 'Musical Lives' or 'requirements' into one list id, or refuse.

    Accepts a raw list id, a list name, or a 'folder/list' pair such as
    'Sudan Memory/Backlog' when the same list name appears in two projects.
    """
    if not ref:
        return None
    r = ref.strip()
    if r.isdigit():
        return r

    lists = _all_lists(team_id)

    if "/" in r:
        left, right = (x.strip().lower() for x in r.split("/", 1))
        hits = [l for l in lists
                if right in l["name"].lower()
                and (left in l["folder"].lower() or left in l["space"].lower())]
    else:
        q = r.lower()
        hits = [l for l in lists if q in l["name"].lower()]
        if not hits:
            # Perhaps they named the project rather than the list inside it
            hits = [l for l in lists if q in l["folder"].lower()]

    if not hits:
        raise ClickUpError(
            f"No list matched '{ref}'. Run clickup_list_lists to see what "
            f"exists."
        )
    if len(hits) > 1:
        lines = "\n".join(
            f"  {l['name']}  (in {l['folder'] or l['space']})" for l in hits[:15]
        )
        raise ClickUpError(
            f"'{ref}' matched {len(hits)} lists. Narrow it, or use "
            f"'folder/list':\n{lines}"
        )
    return hits[0]["id"]


_FOLDER_CACHE: dict[str, dict[str, str]] | None = None  # name -> {id, space}


def _folders(team_id: str | None = None) -> dict[str, dict[str, str]]:
    """Folder (project) names to ids, derived from the list walk."""
    global _FOLDER_CACHE
    if _FOLDER_CACHE is None:
        out: dict[str, dict[str, str]] = {}
        # _all_lists does not keep folder ids; walk folders directly (cached).
        for sp in _request("GET", f"/team/{_team(team_id)}/space").get("spaces") or []:
            sid, sname = str(sp.get("id")), sp.get("name") or ""
            out.setdefault("__spaces__", {})[sname] = sid
            for f in _request("GET", f"/space/{sid}/folder").get("folders") or []:
                out[f.get("name") or ""] = {"id": str(f.get("id")), "space": sname}
        _FOLDER_CACHE = out
    return _FOLDER_CACHE


def _resolve_scope(ref: str | None, team_id: str | None = None) -> dict[str, str] | None:
    """Turn a name into {"kind": list|folder|space, "id", "name"}, or refuse.

    A project name ("Palaeographic Annotator") resolves to its FOLDER, so one
    call covers every list in it. A list name resolves to the list. Exact
    matches win over partial ones, so "Musical Lives" the folder beats
    "Musical Lives - Requirements" the list.
    """
    if not ref:
        return None
    r = ref.strip()
    if r.isdigit():
        return {"kind": "list", "id": r, "name": r}
    q = r.lower()
    folders = _folders(team_id)
    spaces = folders.get("__spaces__", {})

    fx = [(n, v) for n, v in folders.items() if n != "__spaces__" and n.lower() == q]
    if len(fx) == 1:
        return {"kind": "folder", "id": fx[0][1]["id"], "name": fx[0][0]}
    sx = [(n, i) for n, i in spaces.items() if n.lower() == q]
    if len(sx) == 1:
        return {"kind": "space", "id": sx[0][1], "name": sx[0][0]}

    lists = _all_lists(team_id)
    lx = [l for l in lists if l["name"].lower() == q]
    if len(lx) == 1:
        return {"kind": "list", "id": lx[0]["id"], "name": lx[0]["name"]}

    # partial matches: folders first, then lists
    fp = [(n, v) for n, v in folders.items() if n != "__spaces__" and q in n.lower()]
    if len(fp) == 1:
        return {"kind": "folder", "id": fp[0][1]["id"], "name": fp[0][0]}
    lp = [l for l in lists if q in l["name"].lower()]
    if len(lp) == 1 and not fp:
        return {"kind": "list", "id": lp[0]["id"], "name": lp[0]["name"]}
    cands = [f"  project: {n}" for n, _ in fp] + [f"  list: {l['name']} (in {l['folder'] or l['space']})" for l in lp]
    if not cands:
        raise ClickUpError(f"No project, list or space matched '{ref}'. clickup_list_lists shows what exists.")
    raise ClickUpError(f"'{ref}' matched {len(cands)} places. Say which:\n" + "\n".join(cands[:15]))


def _search_tasks(query: str, list_id: str | None, team_id: str | None) -> list[dict[str, Any]]:
    """Find candidate tasks by name.

    If a list is named, search only that list - and without an assignee filter,
    so shared tasks nobody has taken are still found. If no list is named,
    search every task assigned to the authenticated user across the workspace.

    Note there is no short-circuit on the default list: a generic name like
    "Meetings" exists in several projects at once, and quietly preferring the
    default list would write to the wrong one.
    """
    q = query.strip().lower()

    def _pages(path: str, params: list[tuple[str, str]]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for page in range(10):  # hard stop at 1000 tasks
            data = _request("GET", path, params=params + [("page", str(page))])
            batch = data.get("tasks") or []
            tasks.extend(batch)
            if data.get("last_page") or len(batch) < 100:
                break
        return tasks

    base = [("subtasks", "true"), ("include_closed", "true")]

    if list_id:
        tasks = _pages(f"/list/{list_id}/task", base)
    else:
        me = _request("GET", "/user").get("user", {}).get("id")
        if not me:
            return []
        tasks = _pages(f"/team/{_team(team_id)}/task", base + [("assignees[]", str(me))])

    seen: set[str] = set()
    hits: list[dict[str, Any]] = []
    for t in tasks:
        tid = str(t.get("id"))
        if q in (t.get("name") or "").lower() and tid not in seen:
            seen.add(tid)
            hits.append(t)
    return hits


def _resolve_task(task: str, list_ref: str | None, team_id: str | None) -> dict[str, Any]:
    """Turn 'K05' or a raw id into exactly one task, or refuse.

    Refusing on ambiguity is deliberate: writing time or a status change to the
    wrong task in a shared workspace is worse than being asked again.
    """
    ref = task.strip()
    if not ref:
        raise ClickUpError("No task given.")

    # A raw ClickUp id, or one pasted as a full task URL
    m = re.search(r"app\.clickup\.com/t/([0-9a-z]+)", ref, re.I)
    if m:
        ref = m.group(1)
    # ClickUp ids are 6-12 lowercase alphanumerics. A name fragment like "K05"
    # is shorter than that, so length alone separates the two cases; if the
    # guess is wrong the lookup 404s and we fall through to a name search.
    if _ID_RE.fullmatch(ref.lower()):
        try:
            return _request("GET", f"/task/{ref}")
        except ClickUpError as e:
            if e.status not in (404, 401):
                raise  # network, rate limit, auth - not evidence about the id
            # a 404 (or ClickUp's occasional 401-for-unknown-id) just means
            # this was a name fragment - fall through to a name search

    lid = _resolve_list(list_ref, team_id)
    hits = _search_tasks(ref, lid, team_id)
    if not hits:
        where = f"list {lid}" if lid else "every task assigned to you"
        raise ClickUpError(
            f"Nothing matched '{task}' in {where}. Name a list to search there "
            f"instead - clickup_list_lists shows what exists - or pass the "
            f"task id."
        )
    if len(hits) > 1:
        lines = "\n".join("  " + _task_line(t) for t in hits[:10])
        raise ClickUpError(
            f"'{task}' matched {len(hits)} tasks, so nothing was written. "
            f"Say which one:\n{lines}"
        )
    return hits[0]


APP = "https://app.clickup.com"


def _url_task(task_id: str) -> str:
    return f"{APP}/t/{task_id}"


def _url_list(list_id: str, team_id: str | None = None) -> str:
    return f"{APP}/{_team(team_id)}/v/li/{list_id}"


def _url_folder(folder_id: str, team_id: str | None = None) -> str:
    return f"{APP}/{_team(team_id)}/v/f/{folder_id}"


def _url_doc(doc_id: str, team_id: str | None = None) -> str:
    return f"{APP}/{_team(team_id)}/v/dc/{doc_id}"


def _url_rule(kind: str, team_id: str | None = None) -> str:
    """One-line substitution rule for listings, so a URL for any row can be
    formed without printing forty near-identical strings."""
    if kind == "task":
        return f"Task URL: {APP}/t/<id>"
    if kind == "list":
        return f"List URL: {APP}/{_team(team_id)}/v/li/<id>"
    if kind == "doc":
        return f"Doc URL: {APP}/{_team(team_id)}/v/dc/<id>"
    return ""


def _where(t: dict[str, Any]) -> str:
    """The list, and folder if there is one, that a task sits in."""
    lname = (t.get("list") or {}).get("name") or ""
    fname = (t.get("folder") or {}).get("name") or ""
    if fname and fname.lower() != "hidden":
        return f"{fname} / {lname}" if lname else fname
    return lname or "?"


def _task_line(t: dict[str, Any]) -> str:
    status = (t.get("status") or {}).get("status", "?")
    assignees = ", ".join(a.get("username", "?") for a in t.get("assignees") or []) or "unassigned"
    return f"[{t.get('id')}] {t.get('name')} - {status} - {assignees} - in {_where(t)}"


_MEMBER_CACHE: list[dict[str, Any]] | None = None


def _members(team_id: str | None = None) -> list[dict[str, Any]]:
    """Workspace members (id, username, email), cached per process."""
    global _MEMBER_CACHE
    if _MEMBER_CACHE is not None:
        return _MEMBER_CACHE
    out: list[dict[str, Any]] = []
    tid = _team(team_id)
    for team in _request("GET", "/team").get("teams") or []:
        if str(team.get("id")) != tid:
            continue
        for m in team.get("members") or []:
            u = m.get("user") or {}
            if u.get("id"):
                out.append({"id": str(u["id"]), "username": u.get("username") or "",
                            "email": u.get("email") or ""})
    _MEMBER_CACHE = out
    return out


def _resolve_person(ref: str, team_id: str | None = None) -> dict[str, Any]:
    """Turn 'Geoffroy' or an email into one workspace member, or refuse."""
    r = ref.strip().lower()
    if not r:
        raise ClickUpError("No person given.")
    if r in ("me", "myself"):
        me = _request("GET", "/user").get("user", {})
        return {"id": str(me.get("id")), "username": me.get("username") or "me", "email": ""}
    people = _members(team_id)
    if r.isdigit():
        for p in people:
            if p["id"] == r:
                return p
    hits = [p for p in people if r in p["username"].lower() or r in p["email"].lower()]
    if not hits:
        raise ClickUpError(
            f"No workspace member matched '{ref}'. clickup_list_members shows who exists."
        )
    if len(hits) > 1:
        lines = "\n".join(f"  {p['username']}  ({p['email']})" for p in hits[:15])
        raise ClickUpError(f"'{ref}' matched {len(hits)} members. Say which:\n{lines}")
    return hits[0]


# --------------------------------------------------------------------------
# Connection and discovery
# --------------------------------------------------------------------------

@tool
def clickup_whoami() -> str:
    """Check the token works and show the authenticated user and workspaces.

    Run this first after setup. It is the quickest way to tell an auth
    problem from a network problem.
    """
    user = _request("GET", "/user").get("user", {})
    teams = _request("GET", "/team").get("teams", [])
    lines = [
        f"Authenticated as {user.get('username')} ({user.get('email')}), user id {user.get('id')}",
        f"Authorization header style in use: {_auth_style}",
        "",
        "Workspaces:",
    ]
    for t in teams:
        marker = "  <- default" if str(t.get("id")) == DEFAULT_TEAM else ""
        lines.append(f"  {t.get('id')}  {t.get('name')}{marker}")
    if not DEFAULT_TEAM:
        lines.append("")
        lines.append("No CLICKUP_TEAM_ID set - add one to the env block to save passing it.")
    return "\n".join(lines)


@tool
def clickup_list_members(query: str = "", team_id: str | None = None) -> str:
    """Show workspace members, so a person can be named in reports.

    query: optional filter on name or email, e.g. "geoff".
    """
    people = _members(team_id)
    q = query.strip().lower()
    if q:
        people = [p for p in people if q in p["username"].lower() or q in p["email"].lower()]
    if not people:
        return f"No members matched '{query}'."
    lines = [f"{len(people)} member(s):"]
    for p in sorted(people, key=lambda x: x["username"].lower()):
        lines.append(f"  {p['username']}  ({p['email']})  [{p['id']}]")
    return "\n".join(lines)


@tool
def clickup_list_lists(query: str = "", team_id: str | None = None) -> str:
    """Show the lists in the workspace, so you can name one in plain English.

    query: optional filter on the list, folder or project name, e.g. "sudan"
    or "musical". Leave empty to see everything, grouped by project.
    """
    lists = _all_lists(team_id)
    q = query.strip().lower()
    if q:
        lists = [
            l for l in lists
            if q in l["name"].lower() or q in l["folder"].lower() or q in l["space"].lower()
        ]
    if not lists:
        return f"No lists matched '{query}'."

    grouped: dict[str, list[dict[str, str]]] = {}
    for l in lists:
        grouped.setdefault(l["folder"] or l["space"], []).append(l)

    out = [f"{len(lists)} list(s). {_url_rule('list', team_id)}"]
    for group in sorted(grouped):
        out.append(f"\n{group}")
        for l in sorted(grouped[group], key=lambda x: x["name"]):
            marker = "  <- default" if l["id"] == DEFAULT_LIST else ""
            out.append(f"    {l['name']}   [{l['id']}]{marker}")
    return "\n".join(out)


@tool
def clickup_list_statuses(list_name: str | None = None, team_id: str | None = None) -> str:
    """Show the status names a list actually accepts.

    list_name: a list name ("Musical Lives"), a "project/list" pair, or an id.
    Defaults to CLICKUP_DEFAULT_LIST.

    Status names are per-list and case sensitive on write, so check here
    before calling clickup_set_status rather than guessing.
    """
    data = _request("GET", f"/list/{_list(_resolve_list(list_name, team_id))}")
    statuses = data.get("statuses") or []
    out = [f"Statuses for list '{data.get('name')}' ({data.get('id')}). {_url_list(str(data.get('id')), team_id)}"]
    for s in statuses:
        out.append(f"  {s.get('status')}   (type: {s.get('type')})")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Reading tasks
# --------------------------------------------------------------------------

@tool
def clickup_list_tasks(
    list_name: str | None = None,
    statuses: str | None = None,
    include_closed: bool = False,
    subtasks: bool = False,
    assigned_to_me: bool = False,
    team_id: str | None = None,
) -> str:
    """List tasks in a list, a whole project (folder), or a space.

    list_name: a list, a PROJECT name (all its lists in one call), a space,
    or an id. Defaults to CLICKUP_DEFAULT_LIST.
    statuses: comma separated status names to filter by, e.g. "in progress,open".
    Leave empty for all open tasks.
    """
    params: list[tuple[str, str]] = []
    if statuses:
        for s in [x.strip() for x in statuses.split(",") if x.strip()]:
            params.append(("statuses[]", s))
    if include_closed:
        params.append(("include_closed", "true"))
    if subtasks:
        params.append(("subtasks", "true"))
    if assigned_to_me:
        me = _request("GET", "/user").get("user", {}).get("id")
        if me:
            params.append(("assignees[]", str(me)))

    sc = _resolve_scope(list_name, team_id) if list_name else None
    if sc and sc["kind"] == "folder":
        path, extra = f"/team/{_team(team_id)}/task", [("project_ids[]", sc["id"])]
    elif sc and sc["kind"] == "space":
        path, extra = f"/team/{_team(team_id)}/task", [("space_ids[]", sc["id"])]
    else:
        path, extra = f"/list/{_list(sc['id'] if sc else None)}/task", []
    tasks: list[dict[str, Any]] = []
    for page in range(10):  # hard stop at 1000 tasks
        data = _request("GET", path, params=params + extra + [("page", str(page))])
        batch = data.get("tasks") or []
        tasks.extend(batch)
        if data.get("last_page") or len(batch) < 100:
            break
    if not tasks:
        return "No tasks matched."
    return (f"{len(tasks)} task(s). {_url_rule('task')}\n"
            + "\n".join("  " + _task_line(t) for t in tasks))


@tool
def clickup_find_task(
    query: str,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Find tasks by name, to check what a phrase resolves to before writing.

    list_name: optional. Named, only that list is searched, including tasks
    assigned to other people. Omitted, every task assigned to you across the
    workspace is searched.
    """
    hits = _search_tasks(query, _resolve_list(list_name, team_id), team_id)
    if not hits:
        where = f"list '{list_name}'" if list_name else "tasks assigned to you"
        return f"Nothing matched '{query}' in {where}."
    return (f"{len(hits)} match(es) for '{query}'. {_url_rule('task')}\n"
            + "\n".join("  " + _task_line(t) for t in hits))


@tool
def clickup_get_task(
    task: str,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Show the full detail of one task, including time already tracked.

    task: a name fragment ("K05"), a task id, or a pasted task URL.
    """
    r = _resolve_task(task, list_name, team_id)
    t = _request("GET", f"/task/{r['id']}", params={"include_subtasks": "true"})
    lines = [
        f"{t.get('name')}  [{t.get('id')}]",
        f"  status:     {(t.get('status') or {}).get('status')}",
        f"  list:       {(t.get('list') or {}).get('name')} ({(t.get('list') or {}).get('id')})",
        f"  assignees:  {', '.join(a.get('username','?') for a in t.get('assignees') or []) or 'unassigned'}",
        f"  priority:   {(t.get('priority') or {}).get('priority', 'none')}",
        f"  due:        {_fmt_ms(t['due_date']) if t.get('due_date') else 'none'}",
        f"  tracked:    {_dur(t.get('time_spent') or 0)}",
        f"  url:        {t.get('url') or _url_task(str(t.get('id')))}",
    ]
    parent = t.get("parent")
    if parent:
        lines.append(f"  parent:     {parent}  ({_url_task(parent)})")
    subs = t.get("subtasks") or []
    if subs:
        lines.append(f"  subtasks:   {len(subs)}")
        for st in subs[:30]:
            lines.append(f"    [{st.get('id')}] {st.get('name')} - {(st.get('status') or {}).get('status', '?')}")
    cfs = []
    for cf in t.get("custom_fields") or []:
        v = cf.get("value")
        if v in (None, "", [], {}):
            continue
        if cf.get("type") == "drop_down" and isinstance(v, int):
            opts = (cf.get("type_config") or {}).get("options") or []
            v = next((o.get("name") for o in opts if o.get("orderindex") == v), v)
        if isinstance(v, list):
            v = ", ".join(str((x.get("name") if isinstance(x, dict) else x)) for x in v)
        cfs.append(f"    {cf.get('name')}: {v}")
    if cfs:
        lines.append("  fields:")
        lines.extend(cfs)
    desc = (t.get("text_content") or "").strip()
    if desc:
        lines.append("  description:")
        lines.extend("    " + ln for ln in desc.splitlines()[:20])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Writing status
# --------------------------------------------------------------------------

@tool
def clickup_set_status(
    task: str,
    status: str,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Set a task's status.

    task:      a name fragment ("K05"), a task id, or a pasted task URL.
    list_name: optional, to say which project the task is in.
    status: must be one the task's list accepts - run clickup_list_statuses
            if unsure, since the names are case sensitive on write.

    Writes to ClickUp immediately. An ambiguous name writes nothing.
    """
    before = _resolve_task(task, list_name, team_id)
    old = (before.get("status") or {}).get("status")
    t = _request("PUT", f"/task/{before['id']}", json={"status": status})
    new = (t.get("status") or {}).get("status")
    return (f"'{t.get('name')}' [{before['id']}] in {_where(before)}: {old} -> {new}\n"
            f"{_url_task(before['id'])}")


@tool
def clickup_append_description(
    task: str,
    text: str,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Append text to a task's description (the main descriptive text).

    Append-only by design: the existing description is preserved and the new
    text is added at the end under a dated marker. There is deliberately no
    replace - a bad replace destroys a description, a bad append is merely
    untidy. Rewrites belong in the ClickUp UI.

    The result echoes the tail of the description as read back from the API
    after the write, so success is verified, not assumed.
    """
    t = _resolve_task(task, list_name, team_id)

    # Prefer the markdown form so existing formatting survives the round trip.
    full = _request("GET", f"/task/{t['id']}", params={"include_markdown_description": "true"})
    current = full.get("markdown_description")
    use_markdown = current is not None
    if current is None:
        current = full.get("description") or full.get("text_content") or ""

    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    addition = f"\n\n---\n*{stamp}:* {text.strip()}" if use_markdown else (
        f"\n\n{stamp}: {text.strip()}"
    )
    body = {"markdown_content": current + addition} if use_markdown else {
        "description": current + addition
    }
    _request("PUT", f"/task/{t['id']}", json=body)

    # Read back and report what the API now holds - verification, not memory.
    check = _request("GET", f"/task/{t['id']}", params={"include_markdown_description": "true"})
    now = check.get("markdown_description") or check.get("description") or check.get("text_content") or ""
    tail = now[-300:].strip()
    ok = text.strip()[:60] in now
    verdict = "verified in read-back" if ok else (
        "WARNING: appended text not found on read-back - check the task in the UI"
    )
    return (
        f"Description appended on '{t.get('name')}' [{t['id']}] in {_where(t)} "
        f"({verdict}). {_url_task(t['id'])}\nDescription now ends:\n...{tail}"
    )


# --------------------------------------------------------------------------
# Comments (notes on tasks)
# --------------------------------------------------------------------------

@tool
def clickup_add_comment(
    task: str,
    text: str,
    notify_all: bool = False,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Add a note to a task, as a ClickUp comment.

    task: a name fragment, a task id, or a pasted task URL. An ambiguous name
    writes nothing and lists the candidates - for a vague request, find the
    task first (clickup_list_tasks / clickup_find_task), confirm the choice,
    then comment.
    notify_all: leave False unless the intent is to ping every watcher.
    """
    t = _resolve_task(task, list_name, team_id)
    created = _request("POST", f"/task/{t['id']}/comment",
                       json={"comment_text": text, "notify_all": notify_all})
    return (f"Comment added to '{t.get('name')}' [{t['id']}] in {_where(t)}. "
            f"Comment id {created.get('id', '?')}.\n{_url_task(t['id'])}")


@tool
def clickup_get_comments(
    task: str,
    limit: int = 10,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Read the most recent comments (notes) on a task, newest first."""
    t = _resolve_task(task, list_name, team_id)
    comments = _request("GET", f"/task/{t['id']}/comment").get("comments") or []
    if not comments:
        return f"No comments on '{t.get('name')}' [{t['id']}]."
    out = [f"Comments on '{t.get('name')}' [{t['id']}] (newest first). {_url_task(t['id'])}"]
    for c in comments[: max(1, min(int(limit), 50))]:
        who = (c.get("user") or {}).get("username", "?")
        when = _fmt_ms(c.get("date"))
        text = (c.get("comment_text") or "").strip()
        flag = "  [resolved]" if c.get("resolved") else ""
        out.append(f"\n{who}, {when}{flag}  (id {c.get('id')}):\n{text}")
    if len(comments) > limit:
        out.append(f"\n({len(comments) - limit} older comment(s) not shown)")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Structural writes - all two-phase: preview first, act only with confirm=True
# --------------------------------------------------------------------------

_PREVIEW = "PREVIEW - nothing written. Check the target with Neil, then call again with confirm=true."

_NAME_RE = re.compile(r"^([A-Z]{1,4})(\d{2,3})([A-Z]?)\s*-\s*\S")


def _naming_convention(names: list[str]) -> tuple[str, int, str] | None:
    """Detect a 'K05 - Title' style convention among sibling task names.

    Returns (prefix, next_free_number, example) when at least 60% of siblings
    share a prefix family, else None.
    """
    fams: dict[str, list[int]] = {}
    for n in names:
        m = _NAME_RE.match(n.strip())
        if m:
            fams.setdefault(m.group(1), []).append(int(m.group(2)))
    if not names or not fams:
        return None
    prefix, nums = max(fams.items(), key=lambda kv: len(kv[1]))
    if len(nums) < max(3, int(0.6 * len(names))):
        return None
    width = 2
    return prefix, max(nums) + 1, f"{prefix}{max(nums) + 1:0{width}d} - <title>"


@tool
def clickup_create_task(
    list_name: str,
    name: str,
    parent: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    description: str = "",
    confirm: bool = False,
    team_id: str | None = None,
) -> str:
    """Create a task (or a subtask, via parent) in a list. Two-phase.

    First call returns a PREVIEW: the resolved list, parent, and a check of
    the name against the destination's naming convention (e.g. "K05 - Title");
    a name that breaks a clear convention is refused with the next free
    number suggested. Nothing is written until confirm=true.

    parent:   a task name fragment/id to create a subtask under.
    assignee: a member name fragment/email.
    """
    lid = _list(_resolve_list(list_name, team_id))
    listinfo = _request("GET", f"/list/{lid}")
    ptask = _resolve_task(parent, list_name, team_id) if parent else None
    who = _resolve_person(assignee, team_id) if assignee else None

    # Convention check against siblings: the list's top-level tasks, or the
    # parent's existing subtasks.
    if ptask:
        full = _request("GET", f"/task/{ptask['id']}", params={"include_subtasks": "true"})
        siblings = [x.get("name") or "" for x in full.get("subtasks") or []]
    else:
        siblings = [x.get("name") or "" for x in _request(
            "GET", f"/list/{lid}/task", params=[("include_closed", "true"), ("page", "0")]
        ).get("tasks") or []]
    conv = _naming_convention(siblings)
    conv_note = "no clear naming convention among siblings"
    if conv:
        prefix, nxt, example = conv
        m = _NAME_RE.match(name.strip())
        if not m or m.group(1) != prefix:
            return (
                f"REFUSED - name does not follow the destination's convention. "
                f"Siblings there use '{prefix}## - Title'; next free number is "
                f"{prefix}{nxt:02d}. Suggested: '{prefix}{nxt:02d} - {name.strip()}'. "
                f"Nothing written."
            )
        conv_note = f"follows convention {prefix}## (next free was {prefix}{nxt:02d})"

    target = (
        f"  list:    {listinfo.get('name')} [{lid}]  {_url_list(lid, team_id)}\n"
        + (f"  parent:  {ptask.get('name')} [{ptask['id']}]  {_url_task(ptask['id'])}\n" if ptask else "")
        + f"  name:    {name.strip()}  ({conv_note})\n"
        + (f"  status:  {status}\n" if status else "")
        + (f"  assignee: {who['username']}\n" if who else "")
        + (f"  description: {description[:120]}{'...' if len(description) > 120 else ''}\n" if description else "")
    )
    if not confirm:
        return f"{_PREVIEW}\nWould create:\n{target}"

    body: dict[str, Any] = {"name": name.strip()}
    if ptask:
        body["parent"] = ptask["id"]
    if status:
        body["status"] = status
    if who:
        body["assignees"] = [int(who["id"])]
    if description:
        body["markdown_content"] = description
    t = _request("POST", f"/list/{lid}/task", json=body)
    check = _request("GET", f"/task/{t['id']}")
    return (
        f"Created '{check.get('name')}' [{t['id']}] in {_where(check)} "
        f"(verified in read-back). {_url_task(t['id'])}"
    )


@tool
def clickup_set_due(
    task: str,
    due: str | None,
    confirm: bool = False,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Set (or clear, with due=None / "none") a task's due date. Two-phase.

    due: "YYYY-MM-DD", "today", "yesterday", "-3d" (or "+14d" style is not
    supported - give a date). First call previews; confirm=true writes.
    """
    t = _resolve_task(task, list_name, team_id)
    clear = due is None or str(due).strip().lower() in ("none", "clear", "")
    new_ms = None if clear else _ms(str(due))
    old = _fmt_ms(t["due_date"]) if t.get("due_date") else "none"
    new_txt = "none" if clear else _fmt_ms(new_ms)[:10]
    if not confirm:
        return (f"{_PREVIEW}\nWould set due date on '{t.get('name')}' [{t['id']}] in {_where(t)}: "
                f"{old} -> {new_txt}\n{_url_task(t['id'])}")
    _request("PUT", f"/task/{t['id']}", json={"due_date": new_ms, "due_date_time": False})
    check = _request("GET", f"/task/{t['id']}")
    now = _fmt_ms(check["due_date"]) if check.get("due_date") else "none"
    return f"Due date on '{check.get('name')}' [{t['id']}] now {now} (verified). {_url_task(t['id'])}"


@tool
def clickup_assign(
    task: str,
    person: str,
    remove: bool = False,
    confirm: bool = False,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Add (or remove, with remove=true) an assignee on a task. Two-phase."""
    t = _resolve_task(task, list_name, team_id)
    who = _resolve_person(person, team_id)
    current = ", ".join(a.get("username", "?") for a in t.get("assignees") or []) or "unassigned"
    verb = "remove" if remove else "add"
    if not confirm:
        return (f"{_PREVIEW}\nWould {verb} {who['username']} on '{t.get('name')}' [{t['id']}] "
                f"in {_where(t)} (currently: {current})\n{_url_task(t['id'])}")
    body = {"assignees": {"rem" if remove else "add": [int(who["id"])]}}
    _request("PUT", f"/task/{t['id']}", json=body)
    check = _request("GET", f"/task/{t['id']}")
    now = ", ".join(a.get("username", "?") for a in check.get("assignees") or []) or "unassigned"
    return f"Assignees on '{check.get('name')}' [{t['id']}] now: {now} (verified). {_url_task(t['id'])}"


@tool
def clickup_delete_time_entry(entry_id: str, confirm: bool = False, team_id: str | None = None) -> str:
    """Delete one of YOUR OWN time entries by id. Two-phase.

    Find ids with clickup_time_report(detail=true). The first call shows what
    would be deleted; confirm=true deletes. Entries belonging to anyone else
    are refused outright.
    """
    tid = _team(team_id)
    data = _request("GET", f"/team/{tid}/time_entries/{entry_id}").get("data") or {}
    if not data:
        raise ClickUpError(f"No time entry with id {entry_id}.")
    me = str(_request("GET", "/user").get("user", {}).get("id"))
    owner = str((data.get("user") or {}).get("id"))
    if owner != me:
        raise ClickUpError(
            f"Entry {entry_id} belongs to {(data.get('user') or {}).get('username', 'someone else')}, "
            f"not you. Refused."
        )
    t = data.get("task") or {}
    desc = (f"{_dur(data.get('duration'))} on '{t.get('name') or '(no task)'}' "
            f"starting {_fmt_ms(data.get('start'))}"
            + (f", \"{data.get('description')}\"" if data.get("description") else ""))
    if not confirm:
        return f"{_PREVIEW}\nWould delete time entry {entry_id}: {desc}"
    _request("DELETE", f"/team/{tid}/time_entries/{entry_id}")
    try:
        _request("GET", f"/team/{tid}/time_entries/{entry_id}")
        gone = "WARNING: entry still readable after delete - check the UI"
    except ClickUpError:
        gone = "verified gone"
    return f"Deleted time entry {entry_id} ({desc}) - {gone}."


@tool
def clickup_delete_comment(
    task: str,
    comment_id: str,
    confirm: bool = False,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Delete one of YOUR OWN comments on a task by id. Two-phase.

    Ids are shown by clickup_get_comments. Others' comments are refused.
    """
    t = _resolve_task(task, list_name, team_id)
    comments = _request("GET", f"/task/{t['id']}/comment").get("comments") or []
    c = next((x for x in comments if str(x.get("id")) == str(comment_id)), None)
    if not c:
        raise ClickUpError(f"No comment {comment_id} on '{t.get('name')}'.")
    me = str(_request("GET", "/user").get("user", {}).get("id"))
    if str((c.get("user") or {}).get("id")) != me:
        raise ClickUpError(
            f"Comment {comment_id} is by {(c.get('user') or {}).get('username', 'someone else')}, not you. Refused."
        )
    text = (c.get("comment_text") or "").strip()
    if not confirm:
        return (f"{_PREVIEW}\nWould delete comment {comment_id} on '{t.get('name')}' [{t['id']}]:\n"
                f"  \"{text[:200]}{'...' if len(text) > 200 else ''}\"")
    _request("DELETE", f"/comment/{comment_id}")
    after = _request("GET", f"/task/{t['id']}/comment").get("comments") or []
    gone = "verified gone" if not any(str(x.get("id")) == str(comment_id) for x in after) else "WARNING: still present"
    return f"Deleted comment {comment_id} on '{t.get('name')}' - {gone}. {_url_task(t['id'])}"


# --------------------------------------------------------------------------
# Time tracking
# --------------------------------------------------------------------------

@tool
def clickup_add_time(
    task: str,
    duration: str,
    when: str | None = None,
    description: str = "",
    billable: bool = False,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Add time to a task in one step. The main way to log work.

    Use this rather than timers - nothing has to be started or remembered.

    task:      a name fragment ("K05"), a task id, or a pasted task URL.
    list_name: optional, to say which project the task is in - "Musical Lives",
               or "Sudan Memory/Backlog" when a list name repeats. Without it,
               every task assigned to you is searched.
    duration: "20", "20m", "20 mins", "1h30", "1.5h", "2 hours 15".
    when:     "today", "yesterday", "-3d" or "YYYY-MM-DD". Defaults to the
              period just ended, i.e. now minus the duration.

    If the name matches more than one task, nothing is written and the
    candidates are listed instead.
    """
    # Parse the duration first: a typo should fail immediately rather than
    # after two or three API calls have gone looking for the task.
    mins = _minutes(duration)
    duration_ms = mins * 60 * 1000
    t = _resolve_task(task, list_name, team_id)

    if when:
        start_ms = _ms(when)
        # A bare date resolves to midnight, a silly time to have worked, so
        # nudge it to 09:00. "-3d" is not nudged: it already means "this
        # time of day, three days ago".
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}|today|yesterday", when.strip().lower()):
            start_ms = int(start_ms) + 9 * 60 * 60 * 1000
    else:
        start_ms = int(time.time() * 1000) - duration_ms

    body: dict[str, Any] = {
        "tid": t["id"],
        "start": int(start_ms),
        "duration": duration_ms,
        "billable": billable,
    }
    if description:
        body["description"] = description
    created = _request("POST", f"/team/{_team(team_id)}/time_entries", json=body)
    eid = (created.get("data") or {}).get("id") or created.get("id") or "?"

    return (
        f"Added {_dur(duration_ms)} to '{t.get('name')}' [{t['id']}] "
        f"in {_where(t)}, starting {_fmt_ms(start_ms)}. Entry id {eid}.\n{_url_task(t['id'])}"
    )


@tool
def clickup_start_timer(
    task: str,
    description: str = "",
    billable: bool = False,
    list_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Start a running timer. Prefer clickup_add_time unless you want a live timer.

    Remember this has to be stopped. clickup_current_timer shows a forgotten one.
    """
    t = _resolve_task(task, list_name, team_id)
    body: dict[str, Any] = {"tid": t["id"], "billable": billable}
    if description:
        body["description"] = description
    data = _request("POST", f"/team/{_team(team_id)}/time_entries/start", json=body).get("data", {})
    name = (data.get("task") or {}).get("name") or t.get("name")
    return f"Timer started on '{name}' in {_where(t)} at {_fmt_ms(data.get('start'))}.\n{_url_task(t['id'])}"


@tool
def clickup_stop_timer(team_id: str | None = None) -> str:
    """Stop the timer currently running for the authenticated user."""
    data = _request("POST", f"/team/{_team(team_id)}/time_entries/stop").get("data", {})
    if not data:
        return "No timer was running."
    name = (data.get("task") or {}).get("name", "unknown task")
    return f"Timer stopped on '{name}'. Logged {_dur(data.get('duration'))}."


@tool
def clickup_current_timer(team_id: str | None = None) -> str:
    """Show the timer currently running, if any."""
    data = _request("GET", f"/team/{_team(team_id)}/time_entries/current").get("data")
    if not data:
        return "No timer running."
    name = (data.get("task") or {}).get("name", "unknown task")
    started = data.get("start")
    running = ""
    try:
        running = f", running {_dur(int(time.time() * 1000) - int(started))}"
    except Exception:
        pass
    return f"Running on '{name}' since {_fmt_ms(started)}{running}. {data.get('description') or ''}".strip()


@tool
def clickup_time_report(
    start: str | None = None,
    end: str | None = None,
    list_name: str | None = None,
    task: str | None = None,
    person: str | None = None,
    detail: bool = False,
    team_id: str | None = None,
) -> str:
    """Summarise tracked time over a date range, grouped by task.

    Defaults to the last 7 days for the authenticated user.
    list_name: a list, a PROJECT (folder - covers all its lists), or a space.
               "Palaeographic Annotator" is one call, not one per list.
    person:    a colleague's name fragment or email ("Geoffroy"), subject to
               ClickUp permissions. Ambiguous names refuse and list matches.
    detail:    also list every entry with its id - needed to delete one.
    Rows carry task ids and list names, so no follow-up lookups are needed.
    """
    who = _resolve_person(person, team_id) if person else None
    end_ms = _ms(end)
    if end_ms is not None and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}|today|yesterday", (end or "").strip().lower()
    ):
        # "end: today" should mean the whole of today, not midnight this
        # morning - otherwise the day's entries are silently excluded.
        end_ms += 24 * 60 * 60 * 1000 - 1
    params: dict[str, Any] = {
        "start_date": _ms(start, default_days_back=7),
        "end_date": end_ms or int(time.time() * 1000),
    }
    if who:
        params["assignee"] = who["id"]
    params["include_location_names"] = "true"
    if task:
        params["task_id"] = _resolve_task(task, list_name, team_id)["id"]
    elif list_name:
        sc = _resolve_scope(list_name, team_id)
        if sc:
            params[{"list": "list_id", "folder": "folder_id", "space": "space_id"}[sc["kind"]]] = sc["id"]
    entries = _request("GET", f"/team/{_team(team_id)}/time_entries", params=params).get("data", [])
    label = f" for {who['username']}" if who else ""
    if not entries:
        return (f"No time entries{label} in that range." +
                (" (If you expected some, your ClickUp role may not permit "
                 "viewing colleagues' time entries.)" if who else ""))

    by_task: dict[str, dict[str, Any]] = {}
    total = 0
    for e in entries:
        t = e.get("task") or {}
        tid = str(t.get("id") or "")
        name = t.get("name") or e.get("description") or "(no task)"
        lname = (e.get("task_location") or {}).get("list_name") or ""
        try:
            d = int(e.get("duration") or 0)
        except Exception:
            d = 0
        row = by_task.setdefault(tid or name, {"name": name, "list": lname, "ms": 0, "n": 0})
        row["ms"] += d
        row["n"] += 1
        total += d

    lines = [
        f"Time{label} from {_fmt_ms(params['start_date'])} to {_fmt_ms(params['end_date'])} "
        f"- {len(entries)} entries, {_dur(total)} total. {_url_rule('task')}",
        "",
    ]
    for tid, row in sorted(by_task.items(), key=lambda kv: -kv[1]["ms"]):
        where = f" ({row['list']})" if row["list"] else ""
        idtxt = f" [{tid}]" if tid and tid != row["name"] else ""
        lines.append(f"  {_dur(row['ms']):>10}  {row['name']}{idtxt}{where}  x{row['n']}")

    if detail:
        lines.append("")
        lines.append("Entries (id, date, duration, task, who, description):")
        for e in sorted(entries, key=lambda x: -int(x.get("start") or 0)):
            t = e.get("task") or {}
            who_ = (e.get("user") or {}).get("username", "?")
            lines.append(
                f"  {e.get('id')}  {_fmt_ms(e.get('start'))}  {_dur(e.get('duration')):>8}  "
                f"{t.get('name') or '(no task)'}  - {who_}  {e.get('description') or ''}".rstrip()
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Docs (Project Review Records live here) - ClickUp v3 API
# --------------------------------------------------------------------------

_DOC_ID_RE = re.compile(r"^[0-9a-z]{2,10}-[0-9]{2,12}$")  # e.g. t7z18-89235


def _all_docs(team_id: str | None = None) -> list[dict[str, Any]]:
    """Every doc in the workspace (id and name). Paged; not cached - docs
    change more often than the list hierarchy and the call is cheap."""
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(20):  # hard stop at 2000 docs
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = _request("GET", f"/v3/workspaces/{_team(team_id)}/docs", params=params)
        batch = data.get("docs") or []
        out.extend(batch)
        cursor = data.get("next_cursor") or ""
        if not cursor or not batch:
            break
    return out


def _resolve_doc(ref: str, team_id: str | None = None) -> dict[str, Any]:
    """Turn a doc name fragment, id, or pasted /v/dc/ URL into one doc."""
    r = ref.strip()
    m = re.search(r"/v/dc/([0-9a-z-]+)", r, re.I)
    if m:
        r = m.group(1)
    if _DOC_ID_RE.fullmatch(r.lower()):
        return {"id": r, "name": r}

    docs = _all_docs(team_id)
    q = r.lower()
    hits = [d for d in docs if q in (d.get("name") or "").lower()]
    if not hits:
        raise ClickUpError(
            f"No doc matched '{ref}'. Run clickup_list_docs to see what exists."
        )
    if len(hits) > 1:
        lines = "\n".join(f"  {d.get('name')}  [{d.get('id')}]" for d in hits[:15])
        raise ClickUpError(f"'{ref}' matched {len(hits)} docs. Say which:\n{lines}")
    return hits[0]


def _walk_pages(pages: list[dict[str, Any]], depth: int = 0) -> list[tuple[int, str, str]]:
    """Flatten a doc's page tree to (depth, name, content) in reading order."""
    out: list[tuple[int, str, str]] = []
    for pg in pages or []:
        out.append((depth, pg.get("name") or "(untitled page)", pg.get("content") or ""))
        out.extend(_walk_pages(pg.get("pages") or [], depth + 1))
    return out


@tool
def clickup_list_docs(query: str = "", team_id: str | None = None) -> str:
    """List the docs in the workspace - meeting notes, Project Review Records.

    query: optional filter on the doc name, e.g. "review record" or "musical".
    """
    docs = _all_docs(team_id)
    q = query.strip().lower()
    if q:
        docs = [d for d in docs if q in (d.get("name") or "").lower()]
    if not docs:
        return f"No docs matched '{query}'."
    lines = [f"{len(docs)} doc(s). {_url_rule('doc', team_id)}"]
    for d in sorted(docs, key=lambda x: (x.get("name") or "").lower()):
        lines.append(f"  {d.get('name')}  [{d.get('id')}]")
    return "\n".join(lines)


@tool
def clickup_doc_outline(doc: str, team_id: str | None = None) -> str:
    """Show a doc's page structure (names, nesting, size) without content.

    Use before clickup_read_doc on a large doc, to pick a page or section.
    """
    d = _resolve_doc(doc, team_id)
    data = _request("GET", f"/v3/workspaces/{_team(team_id)}/docs/{d['id']}/pages",
                    params={"max_page_depth": -1, "content_format": "text/plain"})
    pages = data if isinstance(data, list) else (data.get("pages") or [])
    flat = _walk_pages(pages)
    if not flat:
        return f"Doc '{d.get('name')}' [{d['id']}] has no pages."
    out = [f"'{d.get('name')}' [{d['id']}] - {len(flat)} page(s). {_url_doc(d['id'], team_id)}"]
    for depth, name, content in flat:
        incs = re.findall(r"INCREMENT\s*(\d+)", content, re.I)
        inc_note = f"  increments: {', '.join(dict.fromkeys(incs))}" if incs else ""
        out.append(f"{'  ' * (depth + 1)}{name}  ({len(content):,} chars){inc_note}")
    return "\n".join(out)


@tool
def clickup_read_doc(
    doc: str,
    section: str = "",
    team_id: str | None = None,
) -> str:
    """Read a doc as markdown - the way to get at a Project Review Record.

    doc:     a name fragment ("Musical Lives - Documents"), a doc id, or a
             pasted doc URL.
    section: optional. "INCREMENT 4" (or just "4") returns only that section:
             from its heading to the next INCREMENT heading. PRR sections
             accumulate bottom-up, so the latest increment is the highest
             number. Embedded ClickUp tables may render imperfectly as
             markdown.

    Without a section, returns the whole doc (capped at ~40k characters).
    """
    d = _resolve_doc(doc, team_id)
    data = _request(
        "GET",
        f"/v3/workspaces/{_team(team_id)}/docs/{d['id']}/pages",
        params={"max_page_depth": -1, "content_format": "text/md"},
    )
    pages = data if isinstance(data, list) else (data.get("pages") or [])
    flat = _walk_pages(pages)
    if not flat:
        return f"Doc '{d.get('name')}' [{d['id']}] has no readable pages."

    body_parts = []
    for depth, name, content in flat:
        body_parts.append(f"{'#' * min(depth + 1, 4)} {name}\n\n{content}".rstrip())
    body = "\n\n".join(body_parts)

    if section:
        want = re.sub(r"^\s*increment\s*", "", section.strip(), flags=re.I).strip()
        # Find every INCREMENT heading line and slice between them.
        marks = [
            (m.start(), m.group(1))
            for m in re.finditer(r"^[^\S\n]*#*[^\S\n]*INCREMENT[^\S\n]*(\d+)\b.*$",
                                 body, re.I | re.M)
        ]
        if not marks:
            return (
                f"No INCREMENT headings found in '{d.get('name')}'. "
                f"The doc exists but is not sectioned the expected way - "
                f"read it without a section filter to see its structure."
            )
        for i, (pos, num) in enumerate(marks):
            if num == want or f"increment {num}".strip() == section.strip().lower():
                end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
                chunk = body[pos:end].strip()
                return f"'{d.get('name')}' [{d['id']}], INCREMENT {num}. {_url_doc(d['id'], team_id)}\n\n{chunk}"
        available = ", ".join(n for _, n in marks)
        return (
            f"No INCREMENT {want} in '{d.get('name')}'. "
            f"Increments present: {available}."
        )

    cap = 40_000
    if len(body) > cap:
        # The newest PRR increment sits nearest the top, so keep the head
        # and cut the tail.
        body = body[:cap] + ("\n\n[... rest of doc truncated - newest "
                             "content is at the top; ask for a specific "
                             "INCREMENT to see one section in full ...]")
    return f"'{d.get('name')}' [{d['id']}] {_url_doc(d['id'], team_id)}\n\n{body}"


def _selftest() -> int:
    """Check token, network and TLS without involving MCP at all.

    Run: uv run --script clickup_mcp.py --selftest
    """
    print("clickup_mcp self-test")
    print(f"  token present:      {'yes' if TOKEN else 'NO - set CLICKUP_TOKEN'}")
    print(f"  token looks right:  {'yes' if TOKEN.startswith('pk_') else 'no - expected pk_ prefix'}")
    print(f"  default workspace:  {DEFAULT_TEAM or '(unset)'}")
    print(f"  default list:       {DEFAULT_LIST or '(unset)'}")
    print(f"  TLS verify:         {VERIFY!r}")
    print()

    # The @tool wrapper converts ClickUpError into a returned string so MCP
    # clients see the message. Here we need the opposite - failures must
    # raise so the selftest can report FAILED and exit non-zero - so call
    # the originals that functools.wraps stashed on the wrappers.
    def _raw(f):
        return getattr(f, "__wrapped__", f)

    try:
        print(_raw(clickup_whoami)())
    except Exception as e:  # noqa: BLE001 - diagnostic path, show everything
        print(f"FAILED: {e}")
        return 1
    if DEFAULT_LIST:
        print()
        try:
            print(_raw(clickup_list_statuses)())
        except Exception as e:  # noqa: BLE001
            print(f"Status lookup failed: {e}")
            return 1
    if DEFAULT_TEAM:
        print()
        try:
            print(_raw(clickup_current_timer)())
        except Exception as e:  # noqa: BLE001
            print(f"Timer check failed: {e}")
    print()
    print("All good. Register the server and restart your client.")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if not TOKEN:
        print(
            "clickup_mcp: CLICKUP_TOKEN is not set. The server will start but "
            "every call will fail.",
            file=sys.stderr,
        )
    try:
        mcp.run(transport="stdio")
    except TypeError:  # very old 1.x signatures take no transport argument
        mcp.run()
