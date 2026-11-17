#!/usr/bin/env python3
"""
Jira custom field review
========================
Scans every custom field of a Jira Cloud site and writes ONE self-contained HTML report:
screens, contexts, options, issue counts (per field and per option) and where each field is
referenced (saved filters, workflows, automation rules). In the report you can search, filter
and record a decision (delete / keep / merge) per field, then export it to CSV.

Read-only: it only reads from Jira (GET, plus the read-only issue-count search). Nothing is
modified and your API token is never written to disk. Python 3.8+, standard library only.

Needs a Jira administrator account and an API token: https://id.atlassian.com/manage-profile/security/api-tokens

    python jira_custom_fields_review.py [--workers 8] [--out report.html]
"""
import argparse, base64, getpass, http.client, json, os, re, sys, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urlparse

COUNT = "/rest/api/3/search/approximate-count"
LOCK = threading.Lock()


# ---------------------------------------------------------------- terminal output
def say(msg=""):
    print(msg, flush=True)


def fmt_time(s):
    return f"{int(s // 60)}m{int(s % 60):02d}s" if s >= 60 else f"{int(s)}s"


class Bar:
    """One-line progress bar that is safe to update from many threads."""

    def __init__(self, label, total):
        self.label, self.total, self.n, self.t0, self.last = label, max(total, 1), 0, time.time(), 0

    def tick(self):
        with LOCK:
            self.n += 1
            now = time.time()
            if self.n < self.total and now - self.last < 0.25:
                return
            self.last = now
            frac = self.n / self.total
            eta = (now - self.t0) / frac * (1 - frac) if frac else 0
            line = f"  [{'#' * int(frac * 24):<24}] {self.n:>5}/{self.total:<5} {frac:>4.0%}  eta {fmt_time(eta):>6}  {self.label}"
            if sys.stdout.isatty():
                print("\r" + line, end="\n" if self.n >= self.total else "", flush=True)
            elif self.n >= self.total or self.n % max(self.total // 10, 1) == 0:
                print(line, flush=True)


def pmap(label, fn, items, workers):
    """Run fn over items with a worker pool, showing progress; results keep the input order."""
    bar = Bar(label, len(items))

    def run(x):
        try:
            return fn(x)
        finally:
            bar.tick()

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(run, items))


# ---------------------------------------------------------------- Jira client
class Jira:
    def __init__(self, base, email, token):
        self.base = base
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def call(self, path, body=None, url=None):
        """Returns (http_status, json). Retries rate limits and server errors with back-off."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url or self.base + path, data=data, headers={
            "Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, json.loads(r.read() or "null")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                try:
                    return e.code, json.loads(e.read() or "null")
                except ValueError:
                    return e.code, None
            except (OSError, http.client.HTTPException, ValueError):   # dropped connection, timeout, truncated reply
                time.sleep(2 ** attempt)
        return 0, None

    def pages(self, path, size=100):
        """All items of a paginated 'values' endpoint (startAt/maxResults)."""
        out, start, sep = [], 0, "&" if "?" in path else "?"
        while True:
            status, j = self.call(f"{path}{sep}startAt={start}&maxResults={size}")
            vals = (j or {}).get("values", []) if status == 200 else []
            out += vals
            start += len(vals)
            if status != 200 or j.get("isLast") or not vals:
                return out

    def count(self, jql):
        """Approximate number of issues matching a JQL, or (None, error) if it could not be read."""
        status, j = self.call(COUNT, {"jql": jql})
        if status == 200 and isinstance(j, dict) and isinstance(j.get("count"), int):
            return j["count"], None
        return None, f"HTTP {status}"


# ---------------------------------------------------------------- start-up checks
def ask_credentials():
    say("Jira custom field review - read-only scan\n")
    site = os.environ.get("JIRA_URL") or input("Site URL (e.g. mycompany.atlassian.net): ").strip()
    email = os.environ.get("JIRA_EMAIL") or input("Account email: ").strip()
    token = os.environ.get("JIRA_TOKEN") or getpass.getpass("API token (hidden): ").strip()
    host = urlparse(site if "//" in site else "https://" + site).netloc or site
    if "." not in host:
        host += ".atlassian.net"          # allow just the site name
    return "https://" + host, email, token


def check_access(j):
    say("\n[1/6] Checking access")
    status, me = j.call("/rest/api/3/myself")
    if status != 200:
        hint = {401: "wrong email or API token", 403: "access denied", 404: "site URL not found"}.get(status, "no response")
        sys.exit(f"  FAILED (HTTP {status}): {hint}.")
    status, perms = j.call("/rest/api/3/mypermissions?permissions=ADMINISTER,BROWSE_PROJECTS")
    have = {k: v.get("havePermission") for k, v in (perms or {}).get("permissions", {}).items()}
    say(f"  OK  signed in as {me.get('displayName')}")
    if not have.get("ADMINISTER"):
        sys.exit("  FAILED: this account is not a Jira administrator (needed to read field contexts and screens).")
    say("  OK  Jira administrator")
    status, tenant = j.call(None, url=j.base + "/_edge/tenant_info")
    rules_root = None
    if status == 200 and tenant.get("cloudId"):
        rules_root = f"https://api.atlassian.com/automation/public/jira/{tenant['cloudId']}/rest/v1"
        if j.call(None, url=rules_root + "/rule/summary?limit=1")[0] != 200:
            rules_root = None
    say("  OK  automation rules readable" if rules_root else "  !!  automation rules NOT readable - they will not count as usage")
    return rules_root


# ---------------------------------------------------------------- data collection
def load_site(j, rules_root, workers):
    say("\n[2/6] Loading fields, projects, filters, workflows and automation rules")
    fields = j.pages("/rest/api/3/field/search?type=custom&expand=lastUsed,screensCount,contextsCount,isLocked")
    projects = {p["id"]: p for p in j.pages("/rest/api/3/project/search")}
    filters = j.pages("/rest/api/3/filter/search?expand=jql&overrideSharePermissions=true")
    workflows = j.pages("/rest/api/3/workflows/search?expand=values.transitions", size=50)
    issue_types = {str(t["id"]): t["name"] for t in (j.call("/rest/api/3/issuetype")[1] or [])}
    rules = []
    if rules_root:
        url = rules_root + "/rule/summary?limit=100"
        while url:
            _, page = j.call(None, url=url)
            rules += (page or {}).get("data", [])
            nxt = ((page or {}).get("links") or {}).get("next")
            url = rules_root + "/rule/summary" + nxt if nxt and (page or {}).get("data") else None
        raw = pmap("automation rule definitions", lambda r: json.dumps(j.call(None, url=f"{rules_root}/rule/{r['uuid']}")[1], ensure_ascii=False), rules, workers)
        rules = [{"id": r["uuid"], "name": r["name"], "state": r.get("state"), "raw": s} for r, s in zip(rules, raw)]
    say(f"  {len(fields)} custom fields, {len(projects)} projects, {len(filters)} filters, {len(workflows)} workflows, {len(rules)} automation rules")
    return fields, projects, filters, workflows, issue_types, rules


def scan_field(j, f):
    """Contexts, screens, issue count and options of one field."""
    fid, base = f["id"], f"/rest/api/3/field/{f['id']}"
    ctxs = j.pages(f"{base}/context")
    by_ctx = {str(c["id"]): {"id": str(c["id"]), "name": c.get("name", ""), "global": bool(c.get("isGlobalContext")),
                             "anyIssueType": bool(c.get("isAnyIssueType")), "pids": [], "tids": []} for c in ctxs}
    for m in j.pages(f"{base}/context/projectmapping"):
        if str(m.get("contextId")) in by_ctx and m.get("projectId"):
            by_ctx[str(m["contextId"])]["pids"].append(str(m["projectId"]))
    for m in j.pages(f"{base}/context/issuetypemapping"):
        if str(m.get("contextId")) in by_ctx and m.get("issueTypeId"):
            by_ctx[str(m["contextId"])]["tids"].append(str(m["issueTypeId"]))
    screens = [{"id": s["id"], "name": s["name"], "tab": (s.get("tab") or {}).get("name")}
               for s in j.pages(f"{base}/screens")] if f.get("screensCount") else []
    n, err = j.count(f"cf[{fid.split('_')[1]}] IS NOT EMPTY")
    options = {c: j.pages(f"{base}/context/{c}/option") for c in by_ctx} if f.get("areOptionsSupported") else {}
    return {"ctx": list(by_ctx.values()), "screens": screens, "issueCount": n, "countErr": err, "options": options}


def option_counts(j, fields, scans, workers):
    """Issues per option. Fields with no data at all are skipped (every option is 0)."""
    jobs = []
    for f in fields:
        s = scans[f["id"]]
        n = f["id"].split("_")[1]
        flat = [o for opts in s["options"].values() for o in opts]
        for o in {o["id"]: o for o in flat}.values():
            oid = str(o["id"])
            if s["issueCount"] == 0:
                jobs.append((f["id"], oid, None))
            elif o.get("optionId"):                                    # cascading child
                jobs.append((f["id"], oid, f"cf[{n}] in cascadeOption({o['optionId']},{oid})"))
            elif any(str(x.get("optionId")) == oid for x in flat):     # cascading parent
                jobs.append((f["id"], oid, f"cf[{n}] in cascadeOption({oid})"))
            else:
                jobs.append((f["id"], oid, f"cf[{n}] = {oid}"))
    todo = [x for x in jobs if x[2]]
    got = dict(zip([(a, b) for a, b, _ in todo], pmap("issues per option", lambda x: j.count(x[2])[0], todo, workers)))
    return {(a, b): (0 if q is None else got.get((a, b))) for a, b, q in jobs}


def find_references(fields, filters, workflows, rules):
    """Which filters / workflows / automation rules mention each field (by id, cf[id] or quoted name)."""
    by_name = {}
    for f in fields:
        by_name.setdefault(f["name"].strip().lower(), []).append(f["id"])
    refs = {f["id"]: {"filters": [], "workflows": [], "automations": []} for f in fields}
    for flt in filters:
        jql = flt.get("jql") or ""
        ids = {"customfield_" + (a or b) for a, b in re.findall(r"cf\[(\d+)\]|customfield_(\d+)", jql, re.I)}
        for q in re.findall(r'"([^"]+)"|\'([^\']+)\'', jql):
            # Jira may write the type after the name: "Story Points[Number]" -> "Story Points"
            ids |= {i for name in q for i in by_name.get(re.sub(r"\s*\[[^\]]*\]$", "", name).strip().lower(), [])}
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_]*", jql):
            ids |= set(by_name.get(word.lower(), []))
        for i in ids & refs.keys():
            refs[i]["filters"].append({"id": flt["id"], "name": flt["name"]})
    for w in workflows:
        raw = json.dumps(w.get("transitions") or [])
        for i in {"customfield_" + n for n in re.findall(r"customfield_(\d+)", raw, re.I)} & refs.keys():
            refs[i]["workflows"].append({"id": w["id"], "name": w["name"]})
    for f in fields:
        n, name = f["id"].split("_")[1], f["name"].strip()
        quoted = re.compile(re.escape('\\"' + name) + r"(?:\[[^\]\\]*\])?" + re.escape('\\"')) if len(name) > 3 else None
        for r in rules:
            if f["id"] in r["raw"] or f"cf[{n}]" in r["raw"] or (quoted and quoted.search(r["raw"])):
                refs[f["id"]]["automations"].append({"id": r["id"], "name": r["name"], "state": r["state"]})
    return refs


def option_usage(field_id, value, opt_id, refs, filters_by_id, rules_by_id, wf_raw):
    """Filters / workflows / automations (already referencing the field) that also name this option."""
    n, v = field_id.split("_")[1], str(value or "").lower()
    id_clause = re.compile(rf"cf\[{n}\][^\n]{{0,160}}?\b{re.escape(opt_id)}\b")

    def in_jql(jql):
        jql = (jql or "").lower()
        return bool(v and (f'"{v}"' in jql or f"'{v}'" in jql)) or bool(id_clause.search(jql))

    def in_raw(raw):
        return bool(v and json.dumps(value, ensure_ascii=False).lower() in raw.lower()) or f'"{opt_id}"' in raw

    brief = lambda x: {k: x[k] for k in ("id", "name", "state") if k in x}
    return ([brief(x) for x in refs["filters"] if in_jql(filters_by_id[str(x["id"])].get("jql"))],
            [brief(x) for x in refs["workflows"] if in_raw(wf_raw[x["id"]])],
            [brief(x) for x in refs["automations"] if in_raw(rules_by_id[x["id"]]["raw"])])


# ---------------------------------------------------------------- assemble + write
def build_rows(fields, scans, refs, opt_n, projects, extra_projects, filters, workflows, rules):
    filters_by_id = {str(f["id"]): f for f in filters}
    rules_by_id = {r["id"]: r for r in rules}
    wf_raw = {w["id"]: json.dumps(w.get("transitions") or []) for w in workflows}
    def proj(pid):
        p = projects.get(pid) or extra_projects.get(pid) or {}
        return {"id": pid, "key": p.get("key"), "name": p.get("name") or f"Project {pid} (not accessible)"}

    rows = []
    for f in fields:
        s, r = scans[f["id"]], refs[f["id"]]
        ctx = [{"id": c["id"], "name": c["name"], "global": c["global"], "anyIssueType": c["anyIssueType"],
                "projects": [proj(p) for p in dict.fromkeys(c["pids"])],
                "issueTypes": [s["_types"].get(t, f"Issue type {t}") for t in dict.fromkeys(c["tids"])]} for c in s["ctx"]]
        opts = None
        if s["options"]:
            seen, octx = set(), []
            for c in s["ctx"]:
                items = s["options"][c["id"]]
                out = []
                for o in items:
                    oid = str(o["id"])
                    q = {"id": oid, "value": o["value"], "n": opt_n.get((f["id"], oid))}
                    if o.get("disabled"): q["disabled"] = 1
                    if o.get("optionId"): q["parent"] = str(o["optionId"])
                    for key, hits in zip("fwa", option_usage(f["id"], o["value"], oid, r, filters_by_id, rules_by_id, wf_raw)):
                        if hits: q[key] = hits
                    seen.add(oid)
                    out.append(q)
                octx.append({"id": c["id"], "name": c["name"], "global": c["global"], "options": out})
            opts = {"count": len(seen), "contexts": octx}
        rows.append({
            "id": f["id"], "name": f["name"].strip(), "type": f.get("typeDisplayName", ""), "description": f.get("description") or "",
            "locked": bool(f.get("isLocked")), "lastUsed": (f.get("lastUsed") or {}).get("value"),
            "issueCount": s["issueCount"], "countErr": s["countErr"], "screens": s["screens"], "screenCount": len(s["screens"]),
            "ctx": ctx, "contexts": len(ctx), "projects": list({p["id"]: p for c in ctx for p in c["projects"]}.values()),
            "filters": r["filters"], "workflows": r["workflows"], "automations": r["automations"],
            "refCount": sum(len(r[k]) for k in r), "opts": opts})
    return rows


def main():
    ap = argparse.ArgumentParser(description="Scan Jira custom fields and write an HTML review report.")
    ap.add_argument("--workers", type=int, default=8, help="parallel requests (default 8; lower it if Jira rate-limits you)")
    ap.add_argument("--out", help="output file (default: custom-fields-review-<site>-<date>.html)")
    args = ap.parse_args()
    t0 = time.time()

    site, email, token = ask_credentials()
    j = Jira(site, email, token)
    rules_root = check_access(j)
    fields, projects, filters, workflows, issue_types, rules = load_site(j, rules_root, args.workers)

    say("\n[3/6] Scanning fields (contexts, screens, issue counts, options)")
    results = pmap("fields", lambda f: scan_field(j, f), fields, args.workers)
    scans = {f["id"]: dict(s, _types=issue_types) for f, s in zip(fields, results)}
    unknown = {p for s in scans.values() for c in s["ctx"] for p in c["pids"] if p not in projects}
    extra = {p: (j.call(f"/rest/api/3/project/{p}")[1] or {}) for p in unknown}   # archived / hidden projects

    say("\n[4/6] Counting issues per option")
    opt_n = option_counts(j, fields, scans, args.workers)

    say("\n[5/6] Finding where fields are used (filters, workflows, automations)")
    refs = find_references(fields, filters, workflows, rules)
    rows = build_rows(fields, scans, refs, opt_n, projects, extra, filters, workflows, rules)

    say("\n[6/6] Writing the report")
    meta = {"siteUrl": site, "scanned": date.today().isoformat()}
    page = TEMPLATE.replace("__DATA__", lambda_json(rows)).replace("__META__", lambda_json(meta))
    out = args.out or f"custom-fields-review-{urlparse(site).netloc.split('.')[0]}-{date.today()}.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)

    used = sum(1 for r in rows if r["issueCount"] is None or r["issueCount"] > 0 or r["refCount"] > 0)
    say(f"  {out}  ({os.path.getsize(out) // 1024} KB)")
    say(f"\nDone in {fmt_time(time.time() - t0)}: {len(rows)} fields - {used} used (data or referenced), {len(rows) - used} with no data and no references.")
    say("Counts only include issues this account can see. Projects it cannot browse, archived projects")
    say("and issues hidden by issue security are not counted - treat 'Unused' as evidence, not proof.")
    if not rules_root:
        say("Automation rules were NOT checked, so fields used only by automations may show as unused.")


def lambda_json(obj):
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Custom fields review</title>
<style>
:root{
  /* Atlassian Design System token values (light theme) */
  --ds-surface:#FFFFFF; --ds-surface-sunken:#F7F8F9; --ds-surface-raised:#FFFFFF;
  --ds-border:#091E4224; --ds-border-input:#8590A2; --ds-border-focused:#388BFF;
  --ds-text:#172B4D; --ds-text-subtle:#44546F; --ds-text-subtlest:#626F86; --ds-text-disabled:#8590A2;
  --ds-link:#0C66E4; --ds-link-hover:#0055CC;
  --ds-bg-neutral:#091E420F; --ds-bg-neutral-hover:#091E4224; --ds-bg-neutral-pressed:#091E4240;
  --ds-bg-selected:#E9F2FF; --ds-text-selected:#0C66E4; --ds-bg-brand:#0C66E4; --ds-bg-brand-hover:#0055CC;
  --ds-bg-success:#DCFFF1; --ds-text-success:#216E4E;
  --ds-bg-warning:#FFF7D6; --ds-text-warning:#7F5F01;
  --ds-bg-danger:#FFECEB; --ds-text-danger:#AE2E24;
  --ds-bg-discovery:#F3F0FF; --ds-text-discovery:#5E4DB2;
  --ds-shadow-overlay:0 8px 12px #091E4226,0 0 1px #091E424F;
  --ds-radius:3px; --ds-hover:#F1F2F4; --ds-input-bg:#FFFFFF; --ds-input-hover:#F7F8F9;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --ds-surface:#1D2125; --ds-surface-sunken:#161A1D; --ds-surface-raised:#22272B;
  --ds-border:#A6C5E229; --ds-border-input:#738496;
  --ds-text:#B6C2CF; --ds-text-subtle:#9FADBC; --ds-text-subtlest:#8C9BAB; --ds-text-disabled:#596773;
  --ds-link:#579DFF; --ds-link-hover:#85B8FF;
  --ds-bg-neutral:#A1BDD914; --ds-bg-neutral-hover:#A6C5E229; --ds-bg-neutral-pressed:#BFDBF847;
  --ds-bg-selected:#1C2B41; --ds-text-selected:#579DFF;
  --ds-bg-success:#1C3329; --ds-text-success:#4BCE97; --ds-bg-warning:#332E1B; --ds-text-warning:#E2B203;
  --ds-bg-danger:#42221F; --ds-text-danger:#FD9891; --ds-bg-discovery:#2B273F; --ds-text-discovery:#B8ACF6;
  --ds-shadow-overlay:0 8px 12px #0304043C,0 0 1px #03040480;
  --ds-hover:#282E33; --ds-input-bg:#22272B; --ds-input-hover:#2C333A; color-scheme:dark;
}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--ds-surface);color:var(--ds-text);font:14px/20px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Oxygen,Ubuntu,"Fira Sans","Droid Sans","Helvetica Neue",sans-serif}
a{color:var(--ds-link);text-decoration:none}a:hover{color:var(--ds-link-hover);text-decoration:underline}
:focus-visible{outline:2px solid var(--ds-border-focused);outline-offset:1px}
button{font:inherit;color:inherit}
[hidden]{display:none!important}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* top bar */
.page{padding:24px 32px 32px}
.ph{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:16px}
h1{margin:0;font-size:24px;line-height:28px;font-weight:500}
.narrow-only{display:none}

/* buttons / fields */
.btn{display:inline-flex;align-items:center;gap:4px;height:32px;padding:0 10px;border:0;border-radius:var(--ds-radius);background:var(--ds-bg-neutral);font-weight:500;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--ds-bg-neutral-hover)}
.btn:active{background:var(--ds-bg-neutral-pressed)}
.btn[disabled]{color:var(--ds-text-disabled);cursor:not-allowed;background:var(--ds-bg-neutral)}
.btn--primary{background:var(--ds-bg-brand);color:#fff}.btn--primary:hover{background:var(--ds-bg-brand-hover)}
.btn--subtle{background:transparent}.btn--subtle:hover{background:var(--ds-bg-neutral)}
.btn--sm{height:24px;padding:0 6px;font-size:12px}
.input,.select{height:32px;border:1px solid var(--ds-border-input);border-radius:var(--ds-radius);background:var(--ds-input-bg);padding:0 8px;color:var(--ds-text);font:inherit;min-width:0}
.input:hover,.select:hover{background:var(--ds-input-hover)}
.input:focus,.select:focus{outline:2px solid var(--ds-border-focused);outline-offset:-1px;background:var(--ds-input-bg)}
.input::placeholder{color:var(--ds-text-subtlest)}

/* search / filter bar (Jira issue navigator style) */
.fbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px}
.search{position:relative;flex:0 1 240px;min-width:180px}
.search svg{position:absolute;left:8px;top:8px;color:var(--ds-text-subtlest);pointer-events:none}
.search .input{width:100%;padding-left:32px}
.chip{display:inline-flex;align-items:center;height:32px;border:0;border-radius:var(--ds-radius);background:var(--ds-bg-neutral);padding:0 8px;gap:6px;cursor:pointer;font-weight:500;max-width:340px}
.chip:hover{background:var(--ds-bg-neutral-hover)}
.chip[aria-expanded=true]{background:var(--ds-bg-selected);color:var(--ds-text-selected)}
.chip--active{background:var(--ds-bg-selected);color:var(--ds-text-selected)}
.chip__t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip__x{display:inline-flex;width:16px;height:16px;align-items:center;justify-content:center;border-radius:50%;margin-right:-2px}
.chip__x:hover{background:#0C66E433}
.fbar__sp{flex:1}
.fbar__act{display:flex;gap:8px;margin-left:auto}
.pop{position:absolute;z-index:200;background:var(--ds-surface-raised);border-radius:4px;box-shadow:var(--ds-shadow-overlay);min-width:260px;max-width:min(640px,calc(100vw - 16px));max-height:min(480px,80vh);display:flex;flex-direction:column}
.pop__h{display:flex;align-items:center;justify-content:space-between;padding:12px 12px 8px;font-weight:600}
.pop__b{padding:0 12px 12px;overflow:auto;display:flex;flex-direction:column;gap:8px}
.pop__list{overflow:auto;max-height:260px;margin:0 -4px;padding:0 4px}
.opt{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:3px;cursor:pointer}
.opt:hover{background:var(--ds-bg-neutral)}
.opt input{margin:0;width:16px;height:16px;accent-color:var(--ds-bg-brand)}
.opt__l{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opt__n{color:var(--ds-text-subtlest);font-size:12px}
.pop .select,.pop .input{width:100%}
.row2{display:flex;gap:8px;align-items:center}
.dp{position:relative;flex:1;min-width:0}
.dp .input{width:100%;padding-right:30px}
.dp__btn{position:absolute;right:2px;top:2px;width:28px;height:28px;display:flex;align-items:center;justify-content:center;border:0;border-radius:3px;background:transparent;color:var(--ds-text-subtle);cursor:pointer}
.dp__btn:hover{background:var(--ds-bg-neutral)}
.cal{margin-top:8px;padding:8px;border:1px solid var(--ds-border);border-radius:4px;background:var(--ds-surface-raised)}
.cal__h{display:flex;align-items:center;justify-content:space-between;font-weight:600;margin-bottom:4px}
.cal__g{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;text-align:center}
.cal__w{font-size:11px;font-weight:600;color:var(--ds-text-subtlest);padding:4px 0}
.cal__d{height:30px;border:0;border-radius:3px;background:transparent;color:var(--ds-text);cursor:pointer;font-variant-numeric:tabular-nums}
.cal__d:hover{background:var(--ds-bg-neutral-hover)}
.cal__d--out{color:var(--ds-text-disabled)}
.cal__d--today{font-weight:700;text-decoration:underline;text-underline-offset:3px}
.cal__d--sel,.cal__d--sel:hover{background:var(--ds-bg-brand);color:#fff}
.linklist{list-style:none;margin:0;padding:0 12px 12px;overflow:auto}
.linklist li{padding:4px 0;display:flex;gap:6px;align-items:flex-start}
.linklist svg{flex:none;margin-top:3px;color:var(--ds-text-subtlest)}
.menu button{display:flex;width:100%;text-align:left;padding:8px 12px;border:0;background:transparent;cursor:pointer}
.menu button:hover{background:var(--ds-bg-neutral)}
.menu{overflow:auto;padding-bottom:6px}

/* result bar */
.rbar{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:8px;color:var(--ds-text-subtle)}
.rbar b{color:var(--ds-text)}
.tally{display:flex;gap:6px}
.bulk{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;background:var(--ds-bg-selected);border-radius:var(--ds-radius);margin-bottom:8px}

/* table */
/* The page scrolls, not the table: no inner scroll container. */
.tw{min-height:160px}
table{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;border:1px solid var(--ds-border);border-radius:4px}
td,th{overflow-wrap:anywhere}
thead th{position:sticky;top:0;z-index:3;background:var(--ds-surface-sunken);border-bottom:2px solid var(--ds-border);text-align:left;padding:0;font-size:12px;font-weight:600;color:var(--ds-text-subtlest)}
thead th>button,thead th>div{display:flex;align-items:center;gap:4px;width:100%;padding:8px;border:0;background:transparent;cursor:pointer;text-align:left;font-weight:600;color:inherit}
thead th>div{cursor:default}
thead th>button,thead th>div{overflow-wrap:normal;word-break:normal}   /* never split a header word mid-word */
thead th>button:hover{background:var(--ds-bg-neutral);color:var(--ds-text)}
tbody td{padding:8px;border-bottom:1px solid var(--ds-border);vertical-align:top;background:var(--ds-surface)}
tbody tr:hover td{background:var(--ds-hover)}
tbody tr.sel td{background:var(--ds-bg-selected)}
.c-chk input{width:16px;height:16px;accent-color:var(--ds-bg-brand);margin:2px 0 0}
.nm{font-weight:500;word-break:break-word}
.fid{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--ds-text-subtlest)}
.tag{display:inline-block;margin-left:6px;font-size:11px;font-weight:600;color:var(--ds-text-subtle);background:var(--ds-bg-neutral);border-radius:3px;padding:0 4px;vertical-align:1px}
.num{font-variant-numeric:tabular-nums}
.zero{color:var(--ds-text-disabled)}
.cnt{border:0;background:transparent;padding:0 4px;margin:0 -4px;border-radius:3px;color:var(--ds-link);cursor:pointer;font-variant-numeric:tabular-nums;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:3px}
.cnt:hover{background:var(--ds-bg-neutral)}
.muted{color:var(--ds-text-subtlest)}
.sm{font-size:12px;line-height:16px}
.opt-ctx{font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--ds-text-subtlest);margin:12px 0 4px}
.opt-ctx:first-child{margin-top:0}
.opt-row{padding:4px 0}
.opt-row + .opt-row{margin-top:2px}
.opt-grid{display:grid;grid-template-columns:minmax(0,1fr) 76px 92px 60px 92px;gap:8px;align-items:baseline}
.opt-grid .r{text-align:right;white-space:nowrap}
.opt-head{font-size:12px;font-weight:600;color:var(--ds-text-subtlest);padding:0 4px 4px 0}
.opt-exp{margin:2px 0 4px 12px;font-size:12px;overflow-wrap:anywhere}
.opt-row .nm2{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}
.opt-row .refs{margin:2px 0 0 12px;font-size:12px}
.opt-row .refs div{display:block;overflow-wrap:anywhere}
.opt-row .child{color:var(--ds-text-subtlest)}
.lz{display:inline-block;max-width:100%;padding:0 4px;border-radius:3px;font-size:11px;line-height:16px;font-weight:700;text-transform:uppercase;letter-spacing:.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:middle}
.lz--default{background:#DCDFE4;color:#44546F}
.lz--success{background:#1F845A;color:#fff}
.lz--info{background:#0C66E4;color:#fff}
.lz--warning{background:#F5CD47;color:#172B4D}
.lz--danger{background:#C9372C;color:#fff}
.lz--discovery{background:#6E5DC6;color:#fff}
.lz--s{background:var(--ds-bg-neutral);color:var(--ds-text-subtle)}
/* Summary lozenges use the same colours as the Decision selects */
.lz--delete{background:var(--ds-bg-danger);color:var(--ds-text-danger)}
.lz--keep{background:var(--ds-bg-success);color:var(--ds-text-success)}
.lz--merge{background:var(--ds-bg-discovery);color:var(--ds-text-discovery)}
.lz--incomplete{background:var(--ds-bg-warning);color:var(--ds-text-warning)}
.tally{display:flex;gap:6px;flex-wrap:wrap}
.tally .lz{border:0;font-family:inherit;cursor:pointer;padding:2px 6px}
.tally .lz:hover{filter:brightness(.95)}
.tally .lz[aria-pressed=true]{box-shadow:0 0 0 2px var(--ds-border-focused)}
.dsel{width:100%;font-weight:500}
.dsel--delete{background:var(--ds-bg-danger);color:var(--ds-text-danger);border-color:#E2483D}
.dsel--keep{background:var(--ds-bg-success);color:var(--ds-text-success);border-color:#22A06B}
.dsel--merge{background:var(--ds-bg-discovery);color:var(--ds-text-discovery);border-color:#8270DB}
/* Merge target: one pill, the clear (x) lives inside it so the field name gets the room. */
.mrg{display:flex;align-items:stretch;margin-top:6px;max-width:100%;min-width:0;min-height:24px;border-radius:3px;background:var(--ds-bg-discovery);color:var(--ds-text-discovery);font-size:12px;font-weight:500}
.mrg:hover{filter:brightness(.97)}
.mrg .pick{flex:1 1 auto;min-width:0;display:flex;align-items:center;gap:4px;border:0;background:transparent;color:inherit;padding:4px 4px 4px 6px;font:inherit;line-height:16px;cursor:pointer;text-align:left;border-radius:3px 0 0 3px}
.mrg .pick__t{white-space:normal;overflow-wrap:anywhere;min-width:0}
.mrg .clr{flex:none;width:24px;border:0;background:transparent;color:inherit;display:flex;align-items:flex-start;padding-top:4px;justify-content:center;cursor:pointer;border-radius:0 3px 3px 0}
.mrg .clr:hover{background:rgba(94,77,178,.2)}
.mp-item b{font-weight:500}
.mrg .chip--warn{background:var(--ds-bg-warning);color:var(--ds-text-warning)}
.cmt{width:100%;min-width:0;min-height:32px;resize:none;field-sizing:content;border:1px solid transparent;border-radius:var(--ds-radius);padding:5px 8px;font:inherit;background:transparent;color:var(--ds-text)}
.cmt:hover{background:var(--ds-bg-neutral);border-color:transparent}
.cmt:focus{background:var(--ds-input-bg);border-color:var(--ds-border-input);outline:2px solid var(--ds-border-focused);outline-offset:-1px}
.cmt::placeholder{color:var(--ds-text-subtlest)}
.empty{text-align:center;padding:48px 16px;color:var(--ds-text-subtlest)}
.empty h3{margin:0 0 4px;color:var(--ds-text);font-size:16px}
.pager{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px;color:var(--ds-text-subtle);flex-wrap:wrap}
.pager .row2{gap:6px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#172B4D;color:#fff;border-radius:4px;padding:10px 16px;box-shadow:var(--ds-shadow-overlay);z-index:300;display:none}
.mp-item{display:flex;flex-direction:column;align-items:flex-start;gap:2px;width:100%;padding:6px 8px;border:0;background:transparent;border-radius:3px;cursor:pointer;text-align:left}
.mp-item:hover{background:var(--ds-bg-neutral)}
.mp-item span{font-size:12px;color:var(--ds-text-subtlest)}
.empty{position:sticky;left:0;width:min(100%,100vw)}
/* Narrow windows: each field becomes a card instead of a squeezed table row. */
@media (max-width:1100px){
  .narrow-only{display:inline-block}
  table{border:0;table-layout:auto}
  colgroup,thead{display:none}
  table,tbody{display:block}
  tbody tr{position:relative;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px;padding:12px 12px 12px 40px;border:1px solid var(--ds-border);border-radius:4px;margin-bottom:8px;background:var(--ds-surface)}
  tbody tr:hover td,tbody tr.sel td{background:transparent}
  tbody tr.sel{background:var(--ds-bg-selected)}
  tbody td{display:block;border:0;padding:0;min-width:0}
  tbody td::before{content:attr(data-label);display:block;font-size:12px;font-weight:600;color:var(--ds-text-subtlest)}
  td.c-chk{position:absolute;left:12px;top:12px;width:auto}
  td.c-chk::before,td.c-name::before{display:none}
  td.c-name,td.c-cmt{grid-column:1/-1}
}
@media (max-width:760px){.page{padding:16px}.search{flex-basis:100%}}
</style>
</head>
<body>
<main class="page">
  <div class="ph"><h1>Custom fields</h1><div class="tally" id="tally" aria-live="polite"></div></div>

  <div class="fbar" id="fbar" role="search" aria-label="Filter fields"></div>
  <div class="bulk" id="bulk" hidden></div>

  <div class="tw" id="tw"><table aria-label="Custom fields"><colgroup><col style="width:3%"><col style="width:12%"><col style="width:7%"><col style="width:7%"><col style="width:5.5%"><col style="width:6%"><col style="width:5.5%"><col style="width:6.5%"><col style="width:7.5%"><col style="width:5%"><col style="width:6.5%"><col style="width:6.5%"><col style="width:12.5%"><col style="width:9.5%"></colgroup><thead id="thead"></thead><tbody id="tbody"></tbody></table><div class="empty" id="empty" hidden><h3>No fields match your search</h3>Try removing a filter or searching for something else.</div></div>
  <div class="pager" id="pager"></div>
</main>
<div id="popRoot"></div>
<div class="toast" id="toast" role="status"></div>
<input type="file" id="file" accept=".csv,text/csv" hidden>

<script>
const DATA = __DATA__;
const META = __META__;
const SITE = META.siteUrl;
const LS_KEY = 'cf-review-decisions-' + META.siteUrl;
const $ = (id) => document.getElementById(id);
const h = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ---------- icons ---------- */
const IC = {
  search:'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M16.04 14.62a7 7 0 1 0-1.42 1.42l4.67 4.67a1 1 0 0 0 1.42-1.42zM5 10a5 5 0 1 1 10 0 5 5 0 0 1-10 0z"/></svg>',
  chev:'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7.7 9.3a1 1 0 0 0-1.4 1.4l5 5a1 1 0 0 0 1.4 0l5-5a1 1 0 0 0-1.4-1.4L12 13.58z"/></svg>',
  plus:'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13 5a1 1 0 1 0-2 0v6H5a1 1 0 1 0 0 2h6v6a1 1 0 1 0 2 0v-6h6a1 1 0 1 0 0-2h-6z"/></svg>',
  x:'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M5.3 5.3a1 1 0 0 1 1.4 0L12 10.6l5.3-5.3a1 1 0 1 1 1.4 1.4L13.4 12l5.3 5.3a1 1 0 0 1-1.4 1.4L12 13.4l-5.3 5.3a1 1 0 0 1-1.4-1.4l5.3-5.3-5.3-5.3a1 1 0 0 1 0-1.4z"/></svg>',
  ext:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M14 4a1 1 0 1 1 0-2h6a1 1 0 0 1 1 1v6a1 1 0 1 1-2 0V5.4l-8.3 8.3a1 1 0 0 1-1.4-1.4L17.6 4zM5 6a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 1 1 2 0v5a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h5a1 1 0 1 1 0 2z"/></svg>',
  cal:'<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 2a1 1 0 0 1 1 1v1h8V3a1 1 0 1 1 2 0v1h1a3 3 0 0 1 3 3v11a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h1V3a1 1 0 0 1 1-1zM4 10v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8zm0-3v1h16V7a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1z"/></svg>',
  sortU:'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 6l7 9H5z"/></svg>',
  sortD:'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 18l-7-9h14z"/></svg>',
};

/* ---------- Jira links ---------- */
const num = (id) => String(id).replace('customfield_', '');
const L = {
  field: (id) => `${SITE}/secure/admin/ConfigureCustomField!default.jspa?customFieldId=${num(id)}`,
  issues: (id) => `${SITE}/issues/?jql=${encodeURIComponent(`cf[${num(id)}] is not EMPTY ORDER BY updated DESC`)}`,
  screen: (id) => `${SITE}/secure/admin/ConfigureFieldScreen.jspa?id=${id}`,
  filter: (id) => `${SITE}/issues/?filter=${id}`,
  workflow: (name) => `${SITE}/secure/admin/workflows/ViewWorkflowXml.jspa?workflowMode=live&workflowName=${encodeURIComponent(name)}`,
  automation: (id) => `${SITE}/jira/settings/automation#/rule/${id}`,
  project: (key) => `${SITE}/browse/${key}`,
};

/* ---------- derived row data ---------- */
/* Verdicts: Used, Unused, and Review (no data, no references, but Jira has a last-used date). */
const ASSESS = {
  used:   { l:'Used',   t:'info',    o:0 },
  review: { l:'Review', t:'warning', o:1 },   // no issue holds a value and nothing references it, but Jira has a "last used" date
  unused: { l:'Unused', t:'default', o:2 },
};
const DECS = { '':'Undecided', delete:'Delete', keep:'Keep', merge:'Merge' };
const BYID = new Map();
DATA.forEach((r, i) => {
  r.i = i; BYID.set(r.id, r);
  // Used = issues hold a value, or a filter/workflow/automation references it.
  // An issue count that could not be read is not evidence of "unused" (the app also keeps these as Used).
  r.assess = r.issueCount == null || r.issueCount > 0 || r.refCount > 0 ? 'used' : (r.lastUsed ? 'review' : 'unused');
  r.wfN = r.workflows.length; r.autN = r.automations.length; r.fltN = r.filters.length;
  // Options (select lists, radio buttons, checkboxes, cascading): distinct options across all contexts.
  const octx = r.opts ? r.opts.contexts : [];
  r.optN = r.opts ? r.opts.count : 0;
  r.optText = octx.map((c) => c.options.map((o) => o.value).join(' ')).join(' ');
  r.lastTs = r.lastUsed ? Date.parse(r.lastUsed) : null;
});

/* ---------- decisions (kept in this browser, exportable) ---------- */
let DEC = {};
try { DEC = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (e) { DEC = {}; }
const saveDec = () => { try { localStorage.setItem(LS_KEY, JSON.stringify(DEC)); } catch (e) {} };
const dec = (r) => DEC[r.id] || { d:'', m:'', c:'' };
// Values a field matches in the Decision filter: its decision, plus "incomplete" for a merge without a target.
const decVals = (r) => { const d = dec(r); return [d.d || 'undecided'].concat(d.d === 'merge' && !d.m ? ['incomplete'] : []); };
function setDec(id, patch) {
  const cur = Object.assign({ d:'', m:'', c:'' }, DEC[id], patch);
  if (cur.d !== 'merge') cur.m = '';
  if (!cur.d && !cur.m && !cur.c) delete DEC[id]; else DEC[id] = cur;
  saveDec();
}

/* ---------- filter model ---------- */
const countBy = (fn) => { const m = new Map(); DATA.forEach((r) => fn(r).forEach(([v, l]) => { const e = m.get(v) || { v, l, n:0 }; e.n++; m.set(v, e); })); return [...m.values()].sort((a, b) => a.l.localeCompare(b.l)); };
const OPTS = {
  assessment: Object.entries(ASSESS).map(([v, a]) => ({ v, l:a.l, n:DATA.filter((r) => r.assess === v).length })),
  decision: () => Object.entries(DECS).map(([v, l]) => ({ v:v || 'undecided', l, n:DATA.filter((r) => decVals(r).includes(v || 'undecided')).length }))
    .concat([{ v:'incomplete', l:'Merge · incomplete (no target)', n:DATA.filter((r) => decVals(r).includes('incomplete')).length }]),
  type: countBy((r) => [[r.type, r.type]]),
  screen: countBy((r) => r.screens.map((s) => [String(s.id), s.name])),
  project: countBy((r) => r.projects.map((p) => [p.id, p.key ? `${p.name} (${p.key})` : p.name])),
  workflow: countBy((r) => r.workflows.map((w) => [w.id, w.name])),
  automation: countBy((r) => r.automations.map((a) => [a.id, a.name])),
  filter: countBy((r) => r.filters.map((f) => [String(f.id), f.name])),
};
const FILTERS = [
  { key:'assessment', label:'Assessment', kind:'multi', single:true, quick:true, get:(r) => [r.assess] },
  { key:'decision', label:'Decision', kind:'multi', single:true, quick:true, get:(r) => decVals(r) },
  { key:'type', label:'Field type', kind:'multi', single:true, quick:true, search:true, get:(r) => [r.type] },
  { key:'issues', label:'Issues with a value', kind:'number', quick:true, get:(r) => r.issueCount },
  { key:'contextsN', label:'Contexts (count)', kind:'number', get:(r) => r.contexts },
  { key:'optionsN', label:'Options (count)', kind:'number', get:(r) => r.optN },
  { key:'option', label:'Option value', kind:'text', get:(r) => r.optText },
  { key:'project', label:'Project-specific context', kind:'multi', search:true, get:(r) => r.projects.map((p) => p.id) },
  { key:'screensN', label:'Screens (count)', kind:'number', get:(r) => r.screenCount },
  { key:'screen', label:'Screen', kind:'multi', search:true, get:(r) => r.screens.map((s) => String(s.id)) },
  { key:'workflowsN', label:'Workflows (count)', kind:'number', get:(r) => r.wfN },
  { key:'workflow', label:'Workflow', kind:'multi', search:true, get:(r) => r.workflows.map((w) => w.id) },
  { key:'automationsN', label:'Automations (count)', kind:'number', get:(r) => r.autN },
  { key:'automation', label:'Automation rule', kind:'multi', search:true, get:(r) => r.automations.map((a) => a.id) },
  { key:'filtersN', label:'Saved filters (count)', kind:'number', get:(r) => r.fltN },
  { key:'filter', label:'Saved filter', kind:'multi', search:true, get:(r) => r.filters.map((f) => String(f.id)) },
  { key:'lastUsed', label:'Last used', kind:'date', get:(r) => r.lastTs },
  { key:'name', label:'Name', kind:'text', get:(r) => r.name },
  { key:'id', label:'Field ID', kind:'text', get:(r) => r.id },
  { key:'description', label:'Description', kind:'text', get:(r) => r.description },
  { key:'comment', label:'Comment', kind:'text', get:(r) => dec(r).c },
];
const FBY = Object.fromEntries(FILTERS.map((f) => [f.key, f]));
const OPS = {
  multiSingle:[['any','is any of'],['none','is none of']],
  multi:[['any','is any of'],['all','is all of'],['none','is none of']],
  number:[['eq','='],['ne','≠'],['gt','>'],['gte','≥'],['lt','<'],['lte','≤'],['between','between'],['unknown','is unknown']],
  text:[['contains','contains'],['notcontains','does not contain'],['is','is exactly'],['starts','starts with'],['empty','is empty'],['notempty','is not empty']],
  date:[['never','was never written'],['set','has been written'],['before','before'],['after','after'],['between','between'],['within','within the last (days)']],
};
const opsFor = (f) => f.kind === 'multi' ? (f.single ? OPS.multiSingle : OPS.multi) : OPS[f.kind];
const F = {};            // key -> { op, val }
const extra = new Set(); // extra filter chips shown in the bar
const defVal = (f) => f.kind === 'multi' ? [] : { a:'', b:'' };
const stOf = (f) => F[f.key] || (F[f.key] = { op:opsFor(f)[0][0], val:defVal(f) });
function isActive(f) {
  const s = F[f.key]; if (!s) return false;
  const { op, val } = s;
  switch (f.kind) {
    case 'multi': return val.length > 0;
    case 'number': return op === 'unknown' || (op === 'between' ? val.a !== '' && val.b !== '' : val.a !== '');
    case 'text': return op === 'empty' || op === 'notempty' || val.a.trim() !== '';
    case 'date': return op === 'never' || op === 'set' || (op === 'between' ? val.a && val.b : val.a !== '');
  }
}
function matches(f, r) {
  const { op, val } = F[f.key]; const v = f.get(r);
  switch (f.kind) {
    case 'multi':
      if (op === 'any') return val.some((x) => v.includes(x));
      if (op === 'all') return val.every((x) => v.includes(x));
      return !val.some((x) => v.includes(x));
    case 'number': {
      if (op === 'unknown') return v == null;
      if (v == null) return false;
      const a = Number(val.a), b = Number(val.b);
      return { eq:v === a, ne:v !== a, gt:v > a, gte:v >= a, lt:v < a, lte:v <= a, between:v >= Math.min(a, b) && v <= Math.max(a, b) }[op];
    }
    case 'text': {
      const s = String(v || '').toLowerCase(), q = val.a.trim().toLowerCase();
      return { contains:s.includes(q), notcontains:!s.includes(q), is:s === q, starts:s.startsWith(q), empty:s === '', notempty:s !== '' }[op];
    }
    case 'date': {
      if (op === 'never') return v == null;
      if (op === 'set') return v != null;
      if (v == null) return false;
      if (op === 'within') return v >= Date.now() - Number(val.a) * 864e5;
      const a = Date.parse(val.a), b = Date.parse(val.b) + 864e5 - 1;
      if (op === 'before') return v < a;
      if (op === 'after') return v >= a + 864e5;
      return v >= a && v <= b;
    }
  }
}
function optLabel(f, v) { const o = (typeof OPTS[f.key] === 'function' ? OPTS[f.key]() : OPTS[f.key] || []).find((x) => x.v === v); return o ? o.l : v; }
function chipText(f) {
  if (!isActive(f)) return f.label;
  const { op, val } = F[f.key]; const opl = opsFor(f).find((o) => o[0] === op)[1];
  switch (f.kind) {
    case 'multi': { const first = optLabel(f, val[0]).replace(/ \(.*\)$/, ''); return `${f.label}${op === 'any' ? ':' : ' ' + opl}  ${first}${val.length > 1 ? ' +' + (val.length - 1) : ''}`; }
    case 'number': return `${f.label} ${op === 'unknown' ? opl : op === 'between' ? `${val.a}–${val.b}` : opl + ' ' + val.a}`;
    case 'text': return `${f.label} ${opl}${op === 'empty' || op === 'notempty' ? '' : ' “' + val.a + '”'}`;
    case 'date': return `${f.label} ${opl}${['never', 'set'].includes(op) ? '' : op === 'between' ? ` ${val.a} – ${val.b}` : ' ' + val.a}`;
  }
}

/* ---------- state ---------- */
let q = '', sortK = 'name', sortD = 1, page = 0, PS = 50;
const SEL = new Set();
const SORTERS = {
  name:(r) => r.name.toLowerCase(), assess:(r) => ASSESS[r.assess].o, type:(r) => r.type, screenCount:(r) => r.screenCount,
  contexts:(r) => r.contexts, optN:(r) => r.optN, wfN:(r) => r.wfN, autN:(r) => r.autN, fltN:(r) => r.fltN, issueCount:(r) => r.issueCount,
  lastUsed:(r) => r.lastTs, decision:(r) => ['', 'delete', 'keep', 'merge'].indexOf(dec(r).d),
};
function view() {
  const active = FILTERS.filter(isActive);
  const t = q.trim().toLowerCase();
  let rows = DATA.filter((r) => {
    if (t && !(r.name + ' ' + r.id + ' ' + r.type + ' ' + r.description + ' ' + r.screens.map((s) => s.name).join(' ') + ' ' + r.projects.map((p) => (p.key || '') + ' ' + p.name).join(' ') + ' ' + dec(r).c).toLowerCase().includes(t)) return false;
    return active.every((f) => matches(f, r));
  });
  const fn = SORTERS[sortK];
  return rows.sort((a, b) => {
    let x = fn(a), y = fn(b);
    if (x == null && y == null) return a.name.localeCompare(b.name);
    if (x == null) return 1; if (y == null) return -1;       // unknown always last
    const c = typeof x === 'string' ? x.localeCompare(y) : x - y;
    return (c || a.name.localeCompare(b.name)) * sortD;
  });
}

/* ---------- popovers ---------- */
let pop = null;
function closePop() { if (pop) { pop.anchor && pop.anchor.setAttribute('aria-expanded', 'false'); pop.el.remove(); pop = null; } }
function openPop(anchor, build, opts = {}) {
  closePop();
  const el = document.createElement('div');
  el.className = 'pop'; el.setAttribute('role', 'dialog');
  if (opts.label) el.setAttribute('aria-label', opts.label);
  $('popRoot').appendChild(el);
  build(el);
  // The popover always opens directly below its control (never above it, never over it).
  // When it would run past the bottom of the window, the page scrolls to make room instead of flipping.
  let r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  const left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
  el.style.left = (left + window.scrollX) + 'px';
  el.style.top = (r.bottom + 4 + window.scrollY) + 'px';      // absolute page coordinates: scrolls with the page
  const over = el.getBoundingClientRect().bottom - (window.innerHeight - 12);
  if (over > 0) {
    window.scrollBy(0, Math.min(over, Math.max(0, r.top - 12)));
    r = anchor.getBoundingClientRect();
    el.style.top = (r.bottom + 4 + window.scrollY) + 'px';
    const room = window.innerHeight - r.bottom - 16;          // anchor is as high as it can go: shrink to fit
    if (el.getBoundingClientRect().bottom > window.innerHeight - 12) el.style.maxHeight = Math.max(180, room) + 'px';
  }
  anchor.setAttribute('aria-expanded', 'true');
  pop = { el, anchor };
  const f = el.querySelector('input,select,button'); if (f) f.focus({ preventScroll:true });
}
document.addEventListener('mousedown', (e) => { if (pop && !pop.el.contains(e.target) && !(pop.anchor && pop.anchor.contains(e.target))) closePop(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && pop) { const a = pop.anchor; closePop(); a && a.focus && a.focus(); } });
window.addEventListener('resize', closePop);

function linksPop(anchor, title, items) {
  openPop(anchor, (el) => {
    el.innerHTML = `<div class="pop__h">${h(title)}</div><ul class="linklist">${items.map((i) => `<li>${IC.ext}<a href="${h(i.href)}" target="_blank" rel="noopener">${h(i.label)}</a>${i.note ? `<span class="muted">${h(i.note)}</span>` : ''}</li>`).join('')}</ul>`;
  }, { label:title });
}

/* ---------- filter bar ---------- */
function renderBar() {
  const quick = FILTERS.filter((f) => f.quick);
  const shown = FILTERS.filter((f) => !f.quick && (extra.has(f.key) || isActive(f)));
  $('fbar').innerHTML =
    `<div class="search"><label class="sr-only" for="q">Search fields</label>${IC.search}<input class="input" id="q" type="search" placeholder="Search" value="${h(q)}" autocomplete="off"></div>` +
    quick.concat(shown).map((f) => `<button class="chip" data-f="${f.key}" aria-haspopup="dialog" aria-expanded="false"><span class="chip__t"></span>${IC.chev}</button>`).join('') +
    `<button class="chip chip--more" id="more" aria-haspopup="menu" aria-expanded="false">${IC.plus}<span>More filters</span></button>` +
    `<button class="btn btn--subtle" id="clearAll" hidden>Clear all</button>` +
    `<select class="select narrow-only" id="sortSel" aria-label="Sort by">${COLS.filter((c) => c[0]).flatMap(([k, l]) => [[k, 1, l + ' ↑'], [k, -1, l + ' ↓']]).map(([k, d, l]) => `<option value="${k}:${d}"${k === sortK && d === sortD ? ' selected' : ''}>Sort: ${l}</option>`).join('')}</select><span class="fbar__sp"></span>` +
    `<div class="fbar__act"><button class="btn" id="imp">Import decisions</button><button class="btn btn--primary" id="exp">Export CSV</button></div>`;
  refreshChips();
}
function refreshChips() {
  document.querySelectorAll('#fbar .chip[data-f]').forEach((c) => {
    const f = FBY[c.dataset.f]; const act = isActive(f);
    c.classList.toggle('chip--active', act);
    c.querySelector('.chip__t').textContent = chipText(f);
    const x = c.querySelector('.chip__x');
    if (act && !x) { const s = document.createElement('span'); s.className = 'chip__x'; s.title = 'Clear filter'; s.setAttribute('role', 'button'); s.setAttribute('aria-label', 'Clear ' + f.label + ' filter'); s.innerHTML = IC.x; c.insertBefore(s, c.lastElementChild); }
    if (!act && x) x.remove();
  });
  const any = FILTERS.some(isActive) || q.trim();
  const ca = $('clearAll'); if (ca) ca.hidden = !any;
}
$('tally').addEventListener('click', (e) => {
  const b = e.target.closest('[data-tally]'); if (!b) return;
  const st = stOf(FBY.decision), v = b.dataset.tally;
  const same = isActive(FBY.decision) && st.op === 'any' && st.val.length === 1 && st.val[0] === v;
  st.op = 'any'; st.val = same ? [] : [v];            // set the Decision filter to this badge (or clear it if already set)
  closePop(); page = 0; refreshChips(); renderAll(false);
});
$('fbar').addEventListener('change', (e) => { if (e.target.id === 'sortSel') { const [k, d] = e.target.value.split(':'); sortK = k; sortD = +d; renderAll(); } });
$('fbar').addEventListener('input', (e) => { if (e.target.id === 'q') { q = e.target.value; page = 0; refreshChips(); renderAll(false); } });
$('fbar').addEventListener('click', (e) => {
  const x = e.target.closest('.chip__x');
  const chip = e.target.closest('.chip[data-f]');
  if (x && chip) { e.stopPropagation(); const f = FBY[chip.dataset.f]; delete F[f.key]; if (!f.quick) extra.delete(f.key); closePop(); page = 0; renderBar(); renderAll(false); return; }
  if (chip) { const f = FBY[chip.dataset.f]; if (pop && pop.anchor === chip) return closePop(); return openPop(chip, (el) => filterPanel(el, f), { label:f.label + ' filter' }); }
  if (e.target.closest('#more')) {
    const b = $('more'); if (pop && pop.anchor === b) return closePop();
    return openPop(b, (el) => {
      const items = FILTERS.filter((f) => !f.quick && !extra.has(f.key) && !isActive(f));
      el.innerHTML = `<div class="pop__h">Add a filter</div><div class="pop__b" style="padding-bottom:4px"><input class="input" id="mq" placeholder="Search filters" aria-label="Search filters"></div><div class="menu" role="menu">${items.map((f) => `<button role="menuitem" data-add="${f.key}">${h(f.label)}</button>`).join('') || '<div class="muted" style="padding:8px 12px">All filters are in use.</div>'}</div>`;
      el.querySelector('#mq').oninput = (ev) => el.querySelectorAll('[data-add]').forEach((n) => { n.hidden = !n.textContent.toLowerCase().includes(ev.target.value.toLowerCase()); });
      el.querySelector('.menu').onclick = (ev) => { const a = ev.target.closest('[data-add]'); if (!a) return; extra.add(a.dataset.add); closePop(); renderBar(); const c = document.querySelector(`#fbar .chip[data-f="${a.dataset.add}"]`); if (c) c.click(); };
    }, { label:'More filters' });
  }
  if (e.target.closest('#clearAll')) { Object.keys(F).forEach((k) => delete F[k]); extra.clear(); q = ''; page = 0; closePop(); renderBar(); renderAll(false); }
  if (e.target.closest('#exp')) exportCsv();
  if (e.target.closest('#imp')) $('file').click();
});

/* ---------- date picker (ADS-style calendar) ---------- */
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const iso = (y, m, d) => `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
function drawCal(host, value, pick) {
  const today = new Date(); const ti = iso(today.getFullYear(), today.getMonth(), today.getDate());
  const base = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(value + 'T00:00:00') : today;
  let y = base.getFullYear(), m = base.getMonth();
  const paint = () => {
    const first = new Date(y, m, 1).getDay(), days = new Date(y, m + 1, 0).getDate(), prev = new Date(y, m, 0).getDate();
    const cells = [];
    for (let i = first - 1; i >= 0; i--) cells.push([iso(m ? y : y - 1, (m + 11) % 12, prev - i), prev - i, true]);
    for (let d = 1; d <= days; d++) cells.push([iso(y, m, d), d, false]);
    while (cells.length % 7) { const d = cells.length - first - days + 1; cells.push([iso(m === 11 ? y + 1 : y, (m + 1) % 12, d), d, true]); }
    host.innerHTML = `<div class="cal" role="dialog" aria-label="Calendar"><div class="cal__h"><button type="button" class="btn btn--sm btn--subtle" data-m="-1" aria-label="Previous month">‹</button><span>${MONTHS[m]} ${y}</span><button type="button" class="btn btn--sm btn--subtle" data-m="1" aria-label="Next month">›</button></div>`
      + `<div class="cal__g">${['Su','Mo','Tu','We','Th','Fr','Sa'].map((d) => `<span class="cal__w">${d}</span>`).join('')}${cells.map(([v, d, out]) => `<button type="button" class="cal__d${out ? ' cal__d--out' : ''}${v === value ? ' cal__d--sel' : ''}${v === ti ? ' cal__d--today' : ''}" data-d="${v}">${d}</button>`).join('')}</div></div>`;
    host.querySelectorAll('[data-m]').forEach((b) => b.onclick = () => { m += +b.dataset.m; if (m < 0) { m = 11; y--; } if (m > 11) { m = 0; y++; } paint(); });
    host.querySelectorAll('[data-d]').forEach((b) => b.onclick = () => pick(b.dataset.d));
  };
  paint();
}
function dateField(getV, setV, host, label) {
  const w = document.createElement('div'); w.className = 'dp';
  w.innerHTML = `<input class="input" placeholder="YYYY-MM-DD" inputmode="numeric" aria-label="${h(label)}" value="${h(getV())}"><button type="button" class="dp__btn" aria-label="Open calendar">${IC.cal}</button>`;
  const inp = w.querySelector('input');
  const open = () => drawCal(host, getV(), (d) => { setV(d); inp.value = d; host.innerHTML = ''; });
  w.querySelector('.dp__btn').onclick = () => { host.innerHTML ? (host.innerHTML = '') : open(); };
  inp.onfocus = () => { if (!host.innerHTML) open(); };
  inp.oninput = () => { const v = inp.value.trim(); if (/^\d{4}-\d{2}-\d{2}$/.test(v) && !isNaN(Date.parse(v))) { setV(v); open(); } else if (v === '') setV(''); };
  return w;
}
function filterPanel(el, f) {
  const st = stOf(f);
  el.innerHTML = `<div class="pop__h"><span>${h(f.label)}</span><button class="btn btn--sm btn--subtle" data-clear>Clear</button></div><div class="pop__b"><select class="select" aria-label="${h(f.label)} operator" data-op>${opsFor(f).map((o) => `<option value="${o[0]}"${o[0] === st.op ? ' selected' : ''}>${o[1]}</option>`).join('')}</select><div data-val></div></div>`;
  const valBox = el.querySelector('[data-val]');
  const changed = () => { page = 0; refreshChips(); renderAll(false); };
  const drawVal = () => {
    const s = stOf(f);
    if (f.kind === 'multi') {
      const all = typeof OPTS[f.key] === 'function' ? OPTS[f.key]() : OPTS[f.key];
      valBox.innerHTML = (f.search || all.length > 8 ? `<input class="input" data-s placeholder="Search" aria-label="Search options" style="margin-bottom:6px">` : '') + `<div class="pop__list" role="group">${all.map((o) => `<label class="opt" data-l="${h(o.l.toLowerCase())}"><input type="checkbox" value="${h(o.v)}"${s.val.includes(o.v) ? ' checked' : ''}><span class="opt__l" title="${h(o.l)}">${h(o.l)}</span><span class="opt__n">${o.n}</span></label>`).join('') || '<div class="muted">No values in this data set.</div>'}</div>`;
      const sb = valBox.querySelector('[data-s]');
      if (sb) sb.oninput = () => valBox.querySelectorAll('.opt').forEach((n) => { n.hidden = !n.dataset.l.includes(sb.value.toLowerCase()); });
      valBox.onchange = (ev) => { const v = ev.target.value; const set = new Set(s.val); ev.target.checked ? set.add(v) : set.delete(v); s.val = [...set]; changed(); };
      return;
    }
    const op = s.op;
    const noVal = (f.kind === 'number' && op === 'unknown') || (f.kind === 'text' && (op === 'empty' || op === 'notempty')) || (f.kind === 'date' && (op === 'never' || op === 'set'));
    if (noVal) { valBox.innerHTML = ''; return; }
    if (f.kind === 'date' && op !== 'within') {
      // Dates use a calendar date picker (not the browser's native date input).
      valBox.innerHTML = '<div class="row2" data-dr></div><div data-cal></div>';
      const row = valBox.querySelector('[data-dr]'), host = valBox.querySelector('[data-cal]');
      row.appendChild(dateField(() => s.val.a, (v) => { s.val.a = v; changed(); }, host, f.label + ' from'));
      if (op === 'between') {
        row.insertAdjacentHTML('beforeend', '<span class="muted">and</span>');
        row.appendChild(dateField(() => s.val.b, (v) => { s.val.b = v; changed(); }, host, f.label + ' to'));
      }
      return;
    }
    const type = f.kind === 'number' ? 'number' : f.kind === 'date' && op !== 'within' ? 'date' : f.kind === 'date' ? 'number' : 'text';
    const two = op === 'between';
    valBox.innerHTML = `<div class="row2"><input class="input" data-a type="${type}"${type === 'number' ? ' min="0"' : ''} value="${h(s.val.a)}" aria-label="${h(f.label)} value">${two ? `<span class="muted">and</span><input class="input" data-b type="${type}"${type === 'number' ? ' min="0"' : ''} value="${h(s.val.b)}" aria-label="${h(f.label)} upper value">` : ''}</div>`;
    valBox.oninput = (ev) => { if (ev.target.matches('[data-a]')) s.val.a = ev.target.value; if (ev.target.matches('[data-b]')) s.val.b = ev.target.value; changed(); };
  };
  el.querySelector('[data-op]').onchange = (ev) => { const s = stOf(f); s.op = ev.target.value; drawVal(); changed(); };
  el.querySelector('[data-clear]').onclick = () => { delete F[f.key]; stOf(f); drawVal(); el.querySelector('[data-op]').value = stOf(f).op; changed(); };
  drawVal();
}

/* ---------- table ---------- */
const COLS = [
  ['name','Field','c-name'], ['assess','Assessment'], ['type','Type'], ['screenCount','Screens'], ['contexts','Contexts'], ['optN','Options'],
  ['wfN','Workflows'], ['autN','Automations'], ['fltN','Filters'], ['issueCount','Issues with value'], ['lastUsed','Last used'],
  ['decision','Decision','c-dec'], [null,'Comment','c-cmt'],
];
function renderHead(allChecked, some) {
  $('thead').innerHTML = '<tr><th class="c-chk" scope="col"><div><input type="checkbox" id="chkAll" aria-label="Select all on this page"></div></th>' + COLS.map(([k, l, cls]) => {
    if (!k) return `<th scope="col" class="${cls || ''}"><div>${l}</div></th>`;
    const on = sortK === k;
    return `<th scope="col" class="${cls || ''}" aria-sort="${on ? (sortD > 0 ? 'ascending' : 'descending') : 'none'}"><button data-sort="${k}">${l}${on ? (sortD > 0 ? IC.sortU : IC.sortD) : ''}</button></th>`;
  }).join('') + '</tr>';
  const c = $('chkAll'); c.checked = allChecked; c.indeterminate = some && !allChecked;
}
const cell = (n, key, r) => n ? `<button class="cnt" data-pop="${key}" data-i="${r.i}" aria-haspopup="dialog" aria-expanded="false">${n}</button>` : '<span class="zero num">0</span>';
function row(r) {
  const d = dec(r), a = ASSESS[r.assess];
  const tgt = d.m ? BYID.get(d.m) : null;
  return `<tr data-id="${r.id}" class="${SEL.has(r.id) ? 'sel' : ''}">
<td class="c-chk"><input type="checkbox" data-sel aria-label="Select ${h(r.name)}"${SEL.has(r.id) ? ' checked' : ''}></td>
<td class="c-name"><div class="nm"><a href="${h(L.field(r.id))}" target="_blank" rel="noopener" title="Open field and contexts in Jira">${h(r.name)}</a></div><div class="fid">${r.id}</div></td>
<td data-label="Assessment"><span class="lz lz--${a.t}"${r.issueCount == null ? ' title="Issue count could not be read"' : r.assess === 'review' ? ` title="No issue holds a value and nothing references it, but Jira recorded this field in use on ${h(r.lastUsed.slice(0, 10))}"` : ''}>${a.l}</span></td>
<td data-label="Type">${h(r.type)}</td>
<td data-label="Screens">${cell(r.screenCount, 'screens', r)}</td>
<td data-label="Contexts">${cell(r.contexts, 'contexts', r)}</td>
<td data-label="Options">${cell(r.optN, 'options', r)}</td>
<td data-label="Workflows">${cell(r.wfN, 'workflows', r)}</td>
<td data-label="Automations">${cell(r.autN, 'automations', r)}</td>
<td data-label="Filters">${cell(r.fltN, 'filters', r)}</td>
<td data-label="Issues with value">${r.issueCount == null ? `<span class="muted" title="${h(r.countErr || '')}">Unknown</span>` : `<a class="num ${r.issueCount ? '' : 'zero'}" href="${h(L.issues(r.id))}" target="_blank" rel="noopener" title="Open these issues in Jira search">${r.issueCount}</a>`}</td>
<td data-label="Last used" class="muted" style="white-space:nowrap">${r.lastUsed ? h(r.lastUsed.slice(0, 10)) : 'Never'}</td>
<td data-label="Decision" class="c-dec"><select class="select dsel ${d.d ? 'dsel--' + d.d : ''}" data-dec aria-label="Decision for ${h(r.name)}">${Object.entries(DECS).map(([v, l]) => `<option value="${v}"${v === d.d ? ' selected' : ''}>${l}</option>`).join('')}</select>
${d.d === 'merge' ? `<div class="mrg"><button class="pick" data-pick aria-haspopup="dialog" aria-expanded="false" title="${tgt ? h(tgt.name + ' · ' + tgt.id + ' · ' + (tgt.issueCount == null ? 'count unknown' : tgt.issueCount + ' issues')) : ''}"><span class="pick__t">${tgt ? '→ ' + h(tgt.name) : 'Select field'}</span>${tgt ? '' : IC.chev}</button>${tgt ? `<button class="clr" data-unpick aria-label="Clear merge target">${IC.x}</button>` : ''}</div>` : ''}</td>
<td data-label="Comment" class="c-cmt"><textarea class="cmt" data-cmt rows="1" placeholder="Comment" aria-label="Comment for ${h(r.name)}">${h(d.c)}</textarea></td></tr>`;
}
let lastRows = [];
function renderAll(keepPage) {
  lastRows = view();
  const tot = lastRows.length, pages = Math.max(1, Math.ceil(tot / PS));
  if (page >= pages) page = pages - 1;
  const sl = lastRows.slice(page * PS, page * PS + PS);
  const onPage = sl.filter((r) => SEL.has(r.id)).length;
  renderHead(sl.length > 0 && onPage === sl.length, onPage > 0);
  $('tbody').innerHTML = sl.map(row).join('');
  $('empty').hidden = sl.length > 0;
  const t = { delete:0, keep:0, merge:0, incomplete:0 };
  DATA.forEach((r) => { const d = dec(r); if (d.d) t[d.d]++; if (d.d === 'merge' && !d.m) t.incomplete++; });   // merge chosen but no target field yet
  // Each badge is a button: clicking it filters the table to that decision (click again to clear).
  const cur = F.decision && isActive(FBY.decision) && F.decision.val.length === 1 ? F.decision.val[0] : '';
  const badge = (v, cls, n, label, title) => `<button type="button" class="lz lz--${cls}" data-tally="${v}" aria-pressed="${cur === v}" title="${title}">${n} ${label}</button>`;
  $('tally').innerHTML = badge('delete', 'delete', t.delete, 'delete', 'Show fields marked Delete')
    + badge('keep', 'keep', t.keep, 'keep', 'Show fields marked Keep')
    + badge('merge', 'merge', t.merge, 'merge', 'Show fields marked Merge')
    + (t.incomplete ? badge('incomplete', 'incomplete', t.incomplete, 'incomplete', 'Show merges that have no target field yet') : '');
  renderBulk();
  const from = tot ? page * PS + 1 : 0, to = Math.min(tot, page * PS + PS);
  $('pager').innerHTML = `<span>${from}–${to} of ${tot}</span><div class="row2"><label for="ps" class="muted">Rows per page</label><select class="select" id="ps">${[25, 50, 100, 200].map((n) => `<option${n === PS ? ' selected' : ''}>${n}</option>`).join('')}</select><button class="btn" id="pv"${page ? '' : ' disabled'}>Previous</button><span>Page ${page + 1} of ${pages}</span><button class="btn" id="nx"${page < pages - 1 ? '' : ' disabled'}>Next</button></div>`;
}
function renderBulk() {
  const b = $('bulk'); const n = SEL.size;
  if (!n) { b.hidden = true; return; }
  b.hidden = false;
  const more = lastRows.length > n && lastRows.every((r) => !SEL.has(r.id)) ? '' : (lastRows.length > n ? `<button class="btn btn--sm btn--subtle" id="selAll">Select all ${lastRows.length} results</button>` : '');
  b.innerHTML = `<b>${n} selected</b>${more}<span class="fbar__sp"></span><label for="bd" class="muted">Set decision</label><select class="select" id="bd"><option value="">Choose…</option><option value="delete">Delete</option><option value="keep">Keep</option><option value="undecided">Undecided</option></select><button class="btn btn--subtle" id="selClr">Clear selection</button>`;
}
$('thead').addEventListener('click', (e) => {
  const s = e.target.closest('[data-sort]');
  if (s) { const k = s.dataset.sort; if (sortK === k) sortD = -sortD; else { sortK = k; sortD = 1; } closePop(); renderAll(); return; }
  if (e.target.id === 'chkAll') {
    const sl = lastRows.slice(page * PS, page * PS + PS);
    sl.forEach((r) => e.target.checked ? SEL.add(r.id) : SEL.delete(r.id)); renderAll();
  }
});
$('bulk').addEventListener('change', (e) => {
  if (e.target.id !== 'bd' || !e.target.value) return;
  const v = e.target.value === 'undecided' ? '' : e.target.value;
  SEL.forEach((id) => setDec(id, { d:v })); toast(`Decision set for ${SEL.size} fields`); renderAll();
});
$('bulk').addEventListener('click', (e) => {
  if (e.target.id === 'selClr') { SEL.clear(); renderAll(); }
  if (e.target.id === 'selAll') { lastRows.forEach((r) => SEL.add(r.id)); renderAll(); }
});
$('tbody').addEventListener('change', (e) => {
  const tr = e.target.closest('tr'); if (!tr) return; const id = tr.dataset.id;
  if (e.target.matches('[data-sel]')) { e.target.checked ? SEL.add(id) : SEL.delete(id); tr.classList.toggle('sel', e.target.checked); renderBulk(); const sl = lastRows.slice(page * PS, page * PS + PS); const on = sl.filter((r) => SEL.has(r.id)).length; const c = $('chkAll'); c.checked = on === sl.length; c.indeterminate = on > 0 && on < sl.length; }
  if (e.target.matches('[data-dec]')) { setDec(id, { d:e.target.value }); renderAll(); if (e.target.value === 'merge' && !dec(BYID.get(id)).m) { const b = document.querySelector(`tr[data-id="${id}"] [data-pick]`); b && b.click(); } }
});
let cmtT;
$('tbody').addEventListener('input', (e) => {
  if (!e.target.matches('[data-cmt]')) return;
  const id = e.target.closest('tr').dataset.id; clearTimeout(cmtT);
  cmtT = setTimeout(() => setDec(id, { c:e.target.value }), 250);
});
$('tbody').addEventListener('focusout', (e) => { if (e.target.matches('[data-cmt]')) { clearTimeout(cmtT); setDec(e.target.closest('tr').dataset.id, { c:e.target.value }); if (isActive(FBY.comment) || isActive(FBY.decision)) renderAll(); } });
$('tbody').addEventListener('click', (e) => {
  const tr = e.target.closest('tr'); if (!tr) return; const id = tr.dataset.id; const r = BYID.get(id);
  const c = e.target.closest('[data-pop]');
  if (c) {
    if (pop && pop.anchor === c) return closePop();
    const k = c.dataset.pop;
    if (k === 'options') { optionsPop(c, r); return; }
    if (k === 'contexts') { contextsPop(c, r); return; }
    const items = {
      screens:() => r.screens.map((s) => ({ href:L.screen(s.id), label:s.name, note:s.tab ? '· tab ' + s.tab : '' })),
      workflows:() => r.workflows.map((w) => ({ href:L.workflow(w.name), label:w.name })),
      automations:() => r.automations.map((a) => ({ href:L.automation(a.id), label:a.name, note:a.state && a.state !== 'ENABLED' ? '· ' + a.state.toLowerCase() : '' })),
      filters:() => r.filters.map((f) => ({ href:L.filter(f.id), label:f.name })),
    }[k]();
    const title = { screens:'Screens', workflows:'Workflows', automations:'Automation rules', filters:'Saved filters' }[k] + ` · ${items.length}`;
    linksPop(c, title, items); return;
  }
  const p = e.target.closest('[data-pick]');
  if (p) { if (pop && pop.anchor === p) return closePop(); mergePicker(p, r); return; }
  if (e.target.closest('[data-unpick]')) { setDec(id, { m:'' }); renderAll(); }
});
/* Every context of a field: the global one first (links to Jira), then each other context with its projects and issue types. */
function contextsPop(anchor, r) {
  const a = (href, label) => `<a href="${h(href)}" target="_blank" rel="noopener">${h(label)}</a>`;
  const ctxs = r.ctx.slice().sort((x, y) => (y.global - x.global) || x.name.localeCompare(y.name));
  const types = (c) => c.anyIssueType ? 'All issue types' : (c.issueTypes.length ? c.issueTypes.join(', ') : 'No issue types');
  openPop(anchor, (el) => {
    el.innerHTML = `<div class="pop__h"><span>Contexts · ${ctxs.length}</span></div><div class="pop__b" style="width:400px;max-width:100%"><div class="opt-list" style="overflow:auto;max-height:380px">`
      + ctxs.map((c) => {
        // A global context needs no "All projects" / "All issue types" lines; only show what actually narrows it.
        const where = c.projects.length ? c.projects.map((p) => p.key ? a(L.project(p.key), `${p.name} (${p.key})`) : h(p.name)).join(', ')
          : '<span class="muted">Not applied to any project</span>';
        const lines = [
          c.global ? '' : `<div><span class="muted">Projects:</span> ${where}</div>`,
          c.global && c.anyIssueType ? '' : `<div><span class="muted">Issue types:</span> ${h(types(c))}</div>`,
        ].join('');
        return `<div class="opt-row"><div class="nm2">${a(L.field(r.id), c.name || 'Context')}${c.global ? '<span class="lz lz--info">Global</span>' : ''}</div>`
          + (lines ? `<div class="refs">${lines}</div>` : '') + `</div>`;
      }).join('')
      + `</div></div>`;
  }, { label:'Contexts' });
}
/* Options of a field, per context, with the filters / workflows / automations that name each option. */
function optionsPop(anchor, r) {
  const ctxs = r.opts ? r.opts.contexts : [];
  const total = ctxs.reduce((n, c) => n + c.options.length, 0);
  const a = (href, label) => `<a href="${h(href)}" target="_blank" rel="noopener">${h(label)}</a>`;
  openPop(anchor, (el) => {
    const byId = (c) => new Map(c.options.map((o) => [o.id, o]));
    const flat = {};   // key -> option, so a count button can list the filters / workflows / automations behind it
    // Same columns as the main table: usage counts next to the issue count.
    const cnt = (list, kind, key) => list && list.length ? `<button type="button" class="cnt" data-oref="${kind}" data-k="${key}" aria-expanded="false">${list.length}</button>` : '<span class="zero num">0</span>';
    // Breakdown by context: every context gets its own heading with its option count.
    const body = ctxs.map((c, ci) => {
      const m = byId(c);
      return `<div class="opt-ctx">${h(c.name || 'Context')}${c.global ? ' · global' : ''} · ${c.options.length}</div>` + c.options.map((o, oi) => {
        const parent = o.parent && m.get(o.parent);
        flat[ci + ':' + oi] = o;
        // Issues holding this option (same JQL the count came from) and where the option is named.
        const jql = o.parent ? `cf[${num(r.id)}] in cascadeOption(${o.parent},${o.id})` : c.options.some((x) => x.parent === o.id) ? `cf[${num(r.id)}] in cascadeOption(${o.id})` : `cf[${num(r.id)}] = ${o.id}`;
        const n = o.n == null ? '<span class="muted" title="Count unavailable">—</span>' : `<a class="num ${o.n ? '' : 'zero'}" href="${h(`${SITE}/issues/?jql=${encodeURIComponent(jql)}`)}" target="_blank" rel="noopener" title="Open these issues in Jira search">${o.n.toLocaleString('en')} ${o.n === 1 ? 'issue' : 'issues'}</a>`;
        const k = ci + ':' + oi;
        return `<div class="opt-row" data-v="${h(String(o.value).toLowerCase())}"><div class="opt-grid"><span class="nm2">${parent ? `<span class="child">${h(parent.value)} ›</span>` : ''}<span>${h(o.value)}</span>${o.disabled ? '<span class="lz lz--default">Disabled</span>' : ''}</span><span class="r">${cnt(o.w, 'w', k)}</span><span class="r">${cnt(o.a, 'a', k)}</span><span class="r">${cnt(o.f, 'f', k)}</span><span class="r">${n}</span></div><div class="opt-exp" hidden></div></div>`;
      }).join('');
    }).join('');
    el.innerHTML = `<div class="pop__h"><span>Options · ${total}</span><a class="sm" href="${h(L.field(r.id))}" target="_blank" rel="noopener">Edit in Jira</a></div><div class="pop__b" style="width:600px;max-width:100%">${total > 12 ? '<input class="input" data-s placeholder="Search options" aria-label="Search options">' : ''}<div class="opt-grid opt-head"><span>Option</span><span class="r">Workflows</span><span class="r">Automations</span><span class="r">Filters</span><span class="r">Issues</span></div><div class="opt-list" style="overflow:auto;max-height:340px">${body || '<div class="muted">No options.</div>'}</div></div>`;
    const sb = el.querySelector('[data-s]');
    if (sb) sb.oninput = () => el.querySelectorAll('.opt-row').forEach((n) => { n.hidden = !n.dataset.v.includes(sb.value.toLowerCase()); });
    // A usage count lists the filters / workflows / automations behind it, as links, under its option.
    el.addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-oref]'); if (!b) return;
      const row = b.closest('.opt-row'), exp = row.querySelector('.opt-exp'), o = flat[b.dataset.k];
      const open = b.getAttribute('aria-expanded') === 'true';
      row.querySelectorAll('[data-oref]').forEach((x) => x.setAttribute('aria-expanded', 'false'));
      if (open) { exp.hidden = true; return; }
      b.setAttribute('aria-expanded', 'true');
      const kind = b.dataset.oref;
      const items = kind === 'f' ? o.f.map((x) => a(L.filter(x.id), x.name)) : kind === 'w' ? o.w.map((x) => a(L.workflow(x.name), x.name)) : o.a.map((x) => a(L.automation(x.id), x.name));
      exp.innerHTML = `<span class="muted">${{ f:'Filters', w:'Workflows', a:'Automations' }[kind]}:</span> ${items.join(', ')}`;
      exp.hidden = false;
    });
  }, { label:'Options' });
}
function mergePicker(anchor, r) {
  openPop(anchor, (el) => {
    el.innerHTML = `<div class="pop__h">Merge “${h(r.name)}” into</div><div class="pop__b"><input class="input" id="mpq" placeholder="Search fields by name or ID" aria-label="Search fields"><div class="pop__list" id="mpl" role="listbox"></div></div>`;
    const list = el.querySelector('#mpl'), inp = el.querySelector('#mpq');
    const draw = () => {
      const t = inp.value.trim().toLowerCase();
      const c = DATA.filter((x) => x.id !== r.id && !x.locked && (!t || (x.name + ' ' + x.id).toLowerCase().includes(t)))
        .sort((a, b) => a.name.localeCompare(b.name)).slice(0, 60);
      list.innerHTML = c.map((x) => `<button class="mp-item" role="option" data-t="${x.id}"><b>${h(x.name)}</b><span>${x.id} · ${h(x.type)} · ${x.issueCount == null ? 'count unknown' : x.issueCount.toLocaleString('en') + (x.issueCount === 1 ? ' issue' : ' issues')}</span></button>`).join('') || '<div class="muted" style="padding:8px">No matching fields.</div>';
    };
    inp.oninput = draw; draw();
    list.onclick = (ev) => { const b = ev.target.closest('[data-t]'); if (!b) return; setDec(r.id, { d:'merge', m:b.dataset.t }); closePop(); renderAll(); };
  }, { label:'Choose merge target' });
}
$('pager').addEventListener('click', (e) => { if (e.target.id === 'pv') { page--; renderAll(); } if (e.target.id === 'nx') { page++; renderAll(); } });
$('pager').addEventListener('change', (e) => { if (e.target.id === 'ps') { PS = +e.target.value; page = 0; renderAll(); } });

/* ---------- export / import ---------- */
const qq = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
function exportCsv() {
  const head = ['Field ID','Name','Type','Assessment','Issues with value','Screens','Options','Project contexts','Workflows','Automations','Saved filters','Last used','Decision','Merge into (ID)','Merge into (name)','Comment','Issues in Jira'];
  const lines = [head.map(qq).join(',')].concat(lastRows.map((r) => { const d = dec(r), t = d.m && BYID.get(d.m); return [r.id, r.name, r.type, ASSESS[r.assess].l, r.issueCount == null ? '' : r.issueCount, r.screens.map((s) => s.name).join('; '), r.optN, r.projects.map((p) => p.key || p.name).join('; '), r.workflows.map((w) => w.name).join('; '), r.automations.map((a) => a.name).join('; '), r.filters.map((f) => f.name).join('; '), r.lastUsed ? r.lastUsed.slice(0, 10) : '', DECS[d.d], d.m, t ? t.name : '', d.c, L.issues(r.id)].map(qq).join(','); }));
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], { type:'text/csv' })); a.download = 'custom-fields.csv'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000); toast(`Exported ${lastRows.length} fields`);
}
function parseCsv(t) {
  const rows = []; let row = [], f = '', q = false;
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (q) { if (c === '"') { if (t[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
    else if (c === '"') q = true;
    else if (c === ',') { row.push(f); f = ''; }
    else if (c === '\n') { row.push(f); rows.push(row); row = []; f = ''; }
    else if (c !== '\r') f += c;
  }
  if (f || row.length) { row.push(f); rows.push(row); }
  return rows;
}
$('file').addEventListener('change', async (e) => {
  const file = e.target.files[0]; e.target.value = ''; if (!file) return;
  const rows = parseCsv((await file.text()).replace(/^﻿/, ''));
  const hd = rows.shift() || []; const ix = (n) => hd.indexOf(n);
  const iId = ix('Field ID'), iD = ix('Decision'), iM = ix('Merge into (ID)'), iC = ix('Comment');
  if (iId < 0 || iD < 0) return toast('Import failed: needs “Field ID” and “Decision” columns');
  let n = 0;
  rows.forEach((r) => {
    const id = r[iId]; if (!BYID.has(id)) return;
    const dv = Object.entries(DECS).find(([, l]) => l.toLowerCase() === String(r[iD] || '').trim().toLowerCase());
    const m = iM >= 0 && BYID.has(r[iM]) ? r[iM] : '';
    setDec(id, { d:dv ? dv[0] : '', m, c:iC >= 0 ? r[iC] || '' : '' }); n++;
  });
  renderAll(); toast(`Imported decisions for ${n} fields`);
});
let toastT;
function toast(msg) { const t = $('toast'); t.textContent = msg; t.style.display = 'block'; clearTimeout(toastT); toastT = setTimeout(() => t.style.display = 'none', 3200); }

/* ---------- init ---------- */
renderBar(); renderAll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
