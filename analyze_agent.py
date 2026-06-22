#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trade analysis agent — triggered every 3 hours by a Northflank Cron Job.
Flow: pull Logfire trade records -> local attribution stats -> GLM generates an
English report -> push to the GitHub reports/ folder.

All secrets are read from environment variables, never hard-coded. Required:
  LOGFIRE_READ_TOKEN  : Logfire read token (pylf_v2_eu_...)
  GLM_API_KEY         : Zhipu API key (REVOKE the leaked one and regenerate!)
  GITHUB_TOKEN        : GitHub PAT with repo push permission
  GITHUB_REPO         : e.g. montecar100/Trading_setup
"""
import os, sys, json, base64, re
from datetime import datetime, timezone
from collections import Counter, defaultdict
import requests

# ---------- config (all from env vars) ----------
LOGFIRE_TOKEN = os.environ["LOGFIRE_READ_TOKEN"]
GLM_API_KEY   = os.environ["GLM_API_KEY"]
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "montecar100/Trading_setup")

LOGFIRE_BASE = "https://logfire-eu.pydantic.dev/v1/query"
GLM_BASE     = "https://open.bigmodel.cn/api/anthropic/v1/messages"
GLM_MODEL    = os.environ.get("GLM_MODEL", "GLM-5.1")

# ---------- 1. pull Logfire data ----------
def fetch_logfire():
    q = ("SELECT start_timestamp, message, attributes FROM records "
         "WHERE start_timestamp >= NOW() - INTERVAL '24 hour' ORDER BY start_timestamp")
    r = requests.get(LOGFIRE_BASE, headers={"Authorization": LOGFIRE_TOKEN},
                     params={"sql": q}, timeout=60)
    r.raise_for_status()
    data = r.json()
    cols = data.get("columns")
    if not cols:
        return []
    names = [c["name"] for c in cols]
    vals  = [c["values"] for c in cols]
    n = len(vals[0]) if vals else 0
    rows = []
    for i in range(n):
        row = {names[j]: vals[j][i] for j in range(len(names))}
        a = row.get("attributes")
        if isinstance(a, str):
            try: a = json.loads(a)
            except: a = {}
        row["_attr"] = a or {}
        rows.append(row)
    return rows

# ---------- 2. attribution stats (the same analysis we ran by hand) ----------
def analyze(rows):
    out = {}
    out["counts"] = dict(Counter(r["message"] for r in rows))
    out["window"] = (rows[0]["start_timestamp"], rows[-1]["start_timestamp"]) if rows else ("", "")

    exits = [r for r in rows if r["message"] == "exit"]
    # PnL grouped by exit reason
    by_reason = defaultdict(list)
    for r in exits:
        p = r["_attr"].get("pnl")
        if p not in (None, ""):
            try: by_reason[r["_attr"].get("reason", "?")].append(float(p))
            except: pass
    out["exit_by_reason"] = {k: {"n": len(v), "sum": round(sum(v)), "avg": round(sum(v)/len(v))}
                             for k, v in by_reason.items()}
    # hard_stop triggers
    out["hard_stop_count"] = sum(1 for r in exits if r["_attr"].get("reason") == "hard_stop")

    # PnL by symbol + profit factor
    by_sym = defaultdict(list)
    for r in exits:
        p = r["_attr"].get("pnl")
        if p not in (None, ""):
            try: by_sym[r["_attr"].get("symbol")].append(float(p))
            except: pass
    sym_stats = {}
    total = 0
    for s, ps in by_sym.items():
        gp = sum(p for p in ps if p > 0); gl = abs(sum(p for p in ps if p < 0))
        total += sum(ps)
        sym_stats[s] = {"n": len(ps), "net": round(sum(ps)),
                        "win_rate": round(sum(1 for p in ps if p > 0)/len(ps)*100),
                        "pf": round(gp/gl, 2) if gl else 999}
    out["by_symbol"] = sym_stats
    out["total_pnl"] = round(total)

    # reject classification
    rej = [r for r in rows if r["message"] == "reject"]
    def rcls(reason):
        reason = reason or ""
        if "allocator REJECT" in reason: return "allocator(leg budget)"
        if "zero after net-dir" in reason: return "net-dir zeroed"
        if "net-dir" in reason: return "net-dir(shrink only)"
        return "other"
    out["reject_class"] = dict(Counter(rcls(r["_attr"].get("reason")) for r in rej))
    out["reject_total"] = len(rej)

    # realized leverage (parsed from entry 'shrinks' field)
    levs = []
    for r in rows:
        if r["message"] == "entry":
            m = re.search(r'杠杆→([\d.]+)x', r["_attr"].get("shrinks", ""))
            if m: levs.append(float(m.group(1)))
    if levs:
        out["leverage"] = {"min": min(levs), "max": max(levs), "avg": round(sum(levs)/len(levs), 1)}
    return out

# ---------- 3. GLM generates the English report ----------
def gen_report(stats):
    prompt = f"""You are a quant trading systems analyst writing a post-trade review.
Below is the attribution stats (JSON) for the past 24 hours.
Write a concise review report in ENGLISH, in markdown format, covering:
1. One-line overall summary (PnL, profit factor, win rate)
2. Per-leg (symbol) performance comparison
3. Risk-gate (reject) status: is net-dir still zeroing out orders, is allocator still blocking USDJPY
4. How many times the hard_stop (cash-based stop loss) triggered (explain the meaning even if 0)
5. Leverage usage
6. Give 1-2 specific, actionable parameter-tuning suggestions if any. Note: this strategy's core
   philosophy is "guardrails over alpha" and "AI does not generate numerical signals directly", so
   your suggestions are for a human to review — do NOT assume they will be auto-executed.

Stats:
{json.dumps(stats, ensure_ascii=False, indent=2)}

Be professional, concise, data-driven. No filler."""

    r = requests.post(GLM_BASE,
        headers={"Authorization": f"Bearer {GLM_API_KEY}", "Content-Type": "application/json"},
        json={"model": GLM_MODEL, "max_tokens": 2000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120)
    r.raise_for_status()
    data = r.json()
    # Anthropic-format response: content is an array of blocks
    parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
    return "\n".join(parts) if parts else json.dumps(data)[:1000]

# ---------- 4. push report to GitHub ----------
def push_report(md_text):
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    path = f"reports/report_{now}.md"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    content_b64 = base64.b64encode(md_text.encode("utf-8")).decode("ascii")
    r = requests.put(url,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        json={"message": f"analysis report {now}", "content": content_b64},
        timeout=30)
    if r.status_code in (200, 201):
        print(f"OK report pushed: {path}")
    else:
        print(f"ERROR push failed {r.status_code}: {r.text[:300]}")

# ---------- main ----------
def main():
    rows = fetch_logfire()
    print(f"fetched {len(rows)} records")
    if not rows:
        print("no data, skipping")
        return
    stats = analyze(rows)
    print("attribution done:", json.dumps(stats, ensure_ascii=False)[:200])
    report = gen_report(stats)
    header = f"# Trade Review Report\n\nGenerated: {datetime.now(timezone.utc).isoformat()}\n\n"
    header += f"Data window: {stats['window'][0]} -> {stats['window'][1]}\n\n---\n\n"
    push_report(header + report)

if __name__ == "__main__":
    main()
