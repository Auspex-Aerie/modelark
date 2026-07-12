"""Local web app for ModelArk — `modelark serve`.

A localhost server that reads the catalog live and persists your picks to the
`selection` table (which IS the wishlist the fetch pipeline consumes). Browse,
filter, search, tick rows to build a set, watch the per-category tally and the
TB-vs-budget bar. Single-user, stdlib-only.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from modelark.core import db

BUDGET_TB = 27.0
_lock = threading.Lock()
_con = None

_BUCKET = ("CASE WHEN category!='generative-llm' THEN 'non-LLM' "
           "WHEN params_b IS NULL THEN '?' "
           "WHEN params_b<=8 THEN '≤8B' WHEN params_b<=32 THEN '8–32B' "
           "WHEN params_b<=70 THEN '32–70B' WHEN params_b<=200 THEN '70–200B' "
           "ELSE '>200B' END")
_BUCKETS = ["≤8B", "8–32B", "32–70B", "70–200B", ">200B", "non-LLM"]
_SORT = {"id": "repo_id", "p": "params_b", "bucket": "bucket", "cat": "category",
         "v": "variant", "gb": "bytes", "dl": "downloads_30d", "lic": "license"}
_LIMIT = 2000


def _conn():
    global _con
    if _con is None:
        _con = db.connect()  # read-write: the portal writes the selection
    return _con


def _q(sql, params=()):
    with _lock:
        return _conn().execute(sql, list(params)).fetchall()


_total = 0


def build_cache():
    """Materialize v_ui (which aggregates the files table) into a flat in-memory
    table once, so every request is a cheap scan of ~3.5k rows, not a re-join."""
    global _total
    with _lock:
        c = _conn()
        c.execute("DROP TABLE IF EXISTS ui_cache")        # SQLite has no CREATE OR REPLACE TABLE
        c.execute(
            f"CREATE TEMP TABLE ui_cache AS "
            f"SELECT repo_id, author, params_b, category, variant, license, downloads_30d, "
            f"gated, bytes, {_BUCKET} AS bucket FROM v_ui")
        _total = c.execute("SELECT count(*) FROM ui_cache").fetchone()[0]
    return _total


def facets() -> dict:
    cats = _q("SELECT category, count(*) FROM ui_cache GROUP BY 1 ORDER BY 2 DESC")
    lics = _q("SELECT coalesce(license,'—'), count(*) FROM ui_cache GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
    return {
        "categories": [{"name": c, "n": n} for c, n in cats],
        "variants": ["base", "instruct", "reasoning", "finetune", "quant"],
        "buckets": _BUCKETS,
        "licenses": [{"name": l, "n": n} for l, n in lics],
        "budget": BUDGET_TB,
    }


def models(p: dict) -> dict:
    where, params = ["1=1"], []
    if p.get("hide_quant", ["1"])[0] == "1":
        where.append("variant != 'quant'")
    if p.get("hide_gated", ["0"])[0] == "1":
        where.append("NOT gated")
    for field, col in (("cat", "category"), ("v", "variant")):
        if p.get(field, [""])[0]:
            vals = p[field][0].split(",")
            where.append(f"{col} IN ({','.join(['?'] * len(vals))})")
            params += vals
    if p.get("bucket", [""])[0]:
        vals = p["bucket"][0].split(",")
        where.append(f"bucket IN ({','.join(['?'] * len(vals))})")
        params += vals
    if p.get("q", [""])[0].strip():
        where.append("lower(repo_id) LIKE ?")
        params.append(f"%{p['q'][0].strip().lower()}%")
    sort = _SORT.get(p.get("sort", ["dl"])[0], "downloads_30d")
    direction = "ASC" if p.get("dir", ["desc"])[0] == "asc" else "DESC"

    clause = " AND ".join(where)
    base = ("(SELECT ui_cache.*, (sel.repo_id IS NOT NULL) AS sel "
            "FROM ui_cache LEFT JOIN selection sel USING(repo_id))")
    rows = _q(
        f"SELECT repo_id,params_b,bucket,category,variant,license,downloads_30d,gated,bytes,sel "
        f"FROM {base} WHERE {clause} "
        f"ORDER BY {sort} {direction} NULLS LAST LIMIT {_LIMIT}", params)
    matched = _q(f"SELECT count(*) FROM ui_cache WHERE {clause}", params)[0][0]
    total = _total
    keys = ["id", "p", "bucket", "cat", "v", "lic", "dl", "g", "bytes", "sel"]
    return {"rows": [dict(zip(keys, r)) for r in rows], "matched": matched,
            "total": total, "capped": matched > _LIMIT}


def selection() -> dict:
    by = _q("SELECT v.category, count(*), sum(v.bytes) FROM selection s "
            "JOIN ui_cache v USING(repo_id) GROUP BY 1 ORDER BY 3 DESC")
    recent = dict(_q(
        "SELECT category, repo_id FROM (SELECT v.category, s.repo_id, "
        "row_number() OVER (PARTITION BY v.category ORDER BY s.added_at DESC, s.repo_id) rn "
        "FROM selection s JOIN ui_cache v USING(repo_id)) WHERE rn=1"))
    tot = _q("SELECT count(*), coalesce(sum(v.bytes),0) FROM selection s JOIN ui_cache v USING(repo_id)")[0]
    return {
        "n": tot[0], "bytes": tot[1], "budget": BUDGET_TB,
        "by_cat": [{"cat": c, "n": n, "bytes": b, "recent": recent.get(c)} for c, n, b in by],
    }


def toggle(repo_id: str, on: bool) -> dict:
    if on:
        _q("INSERT INTO selection(repo_id) VALUES (?) ON CONFLICT DO NOTHING", [repo_id])
    else:
        _q("DELETE FROM selection WHERE repo_id=?", [repo_id])
    return selection()


def bulk(ids: list[str], on: bool) -> dict:
    with _lock:
        c = _conn()
        if on:
            c.executemany("INSERT INTO selection(repo_id) VALUES (?) ON CONFLICT DO NOTHING",
                          [[i] for i in ids])
        else:
            c.executemany("DELETE FROM selection WHERE repo_id=?", [[i] for i in ids])
    return selection()


def clear() -> dict:
    _q("DELETE FROM selection")
    return selection()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200, headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-response — harmless

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str), "application/json", code)

    def do_GET(self):
        u = urlparse(self.path)
        p = parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(HTML, "text/html; charset=utf-8")
            elif u.path == "/api/facets":
                self._json(facets())
            elif u.path == "/api/models":
                self._json(models(p))
            elif u.path == "/api/selection":
                self._json(selection())
            elif u.path == "/api/export":
                ids = [r[0] for r in _q("SELECT repo_id FROM selection ORDER BY repo_id")]
                self._send(json.dumps(ids, indent=2), "application/json", 200,
                           {"Content-Disposition": "attachment; filename=modelark-selection.json"})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # never crash the server on a bad request
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        u = urlparse(self.path)
        try:
            if u.path == "/api/selection":
                self._json(toggle(body["id"], bool(body["on"])))
            elif u.path == "/api/selection/bulk":
                self._json(bulk(body["ids"], bool(body["on"])))
            elif u.path == "/api/selection/clear":
                self._json(clear())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def serve(port: int = 8077, open_browser: bool = True):
    _conn()  # fail fast if the catalog can't open
    n = build_cache()  # materialize the flat query cache once
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"ModelArk portal: {url}  ({n} models, selection persists to the catalog)")
    print("Ctrl-C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()


HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>ModelArk · Build your set</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e1318;--panel:#151c24;--panel2:#1b232c;--line:#283440;--line2:#364654;
--ink:#e9e6dd;--mut:#94a0ab;--brass:#c9a24b;--steel:#7ab0d6;
--base:#7fb98a;--instruct:#7ab0d6;--reasoning:#c98bd0;--quant:#8a93a0;--finetune:#d09a6a;
--ok:#6fbf73;--warn:#d8a13a;--crit:#d56b6b;}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink);
font-size:13.5px;line-height:1.45;display:flex;height:100vh;overflow:hidden}
.mono{font-family:ui-monospace,Menlo,monospace}button{font:inherit;cursor:pointer}
aside{width:340px;flex:0 0 340px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
.brand{padding:16px 18px 10px}.brand .eb{font-size:.66rem;letter-spacing:.18em;text-transform:uppercase;color:var(--brass);font-weight:600}
.brand h1{font-size:1.15rem;margin:.15rem 0 0}
.budget{padding:12px 18px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.budget .big{font-size:1.75rem;font-variant-numeric:tabular-nums}.budget .of{color:var(--mut);font-size:.85rem}
.bar{height:9px;border-radius:6px;background:var(--panel2);margin:8px 0 4px;overflow:hidden;border:1px solid var(--line)}
.bar>div{height:100%;background:var(--ok);transition:width .2s,background .2s;width:0}
.budget small{color:var(--mut);font-variant-numeric:tabular-nums}
.tally{flex:1;overflow-y:auto;padding:6px 10px 10px}
.tally h2{font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);margin:12px 8px 6px}
.crow{display:grid;grid-template-columns:1fr auto;gap:2px 8px;padding:7px 9px;border-radius:8px}
.crow:hover{background:var(--panel2)}.crow .cc{font-weight:600;text-transform:capitalize}
.crow .cn{font-variant-numeric:tabular-nums}.crow .rec{grid-column:1/3;color:var(--mut);font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acts{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--line)}
.acts button{flex:1;background:var(--panel2);color:var(--ink);border:1px solid var(--line2);border-radius:8px;padding:8px;transition:.15s}
.acts button:hover{border-color:var(--brass);color:var(--brass)}.acts .clear:hover{border-color:var(--crit);color:var(--crit)}
main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.controls{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:9px}
.searchrow{display:flex;gap:10px;align-items:center}
#search{flex:1;background:var(--panel);border:1px solid var(--line2);border-radius:8px;color:var(--ink);padding:9px 12px;font:inherit}
#search:focus{outline:none;border-color:var(--brass)}
.shown{color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.flabel{font-size:.64rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);margin:0 2px 0 6px}
.chip{background:var(--panel);border:1px solid var(--line2);color:var(--mut);border-radius:999px;padding:3px 11px;font-size:.8rem;transition:.12s}
.chip:hover{color:var(--ink)}.chip.on{background:var(--brass);border-color:var(--brass);color:#1a1206;font-weight:600}
.chip.v.on{color:#10160d!important;font-weight:600}
.toggle.on{background:var(--steel);border-color:var(--steel);color:#06131c;font-weight:600}
.bulk{margin-left:auto;display:flex;gap:6px}
.bulk button{background:var(--panel);border:1px solid var(--line2);color:var(--steel);border-radius:8px;padding:4px 10px;font-size:.8rem}
.bulk button:hover{border-color:var(--steel)}
.tablewrap{flex:1;overflow:auto}table{border-collapse:collapse;width:100%;font-size:.85rem}
thead th{position:sticky;top:0;background:var(--panel2);z-index:1;text-align:left;font-size:.64rem;letter-spacing:.07em;
text-transform:uppercase;color:var(--mut);font-weight:600;padding:9px 10px;border-bottom:1px solid var(--line2);white-space:nowrap;cursor:pointer;user-select:none}
thead th.num{text-align:right}thead th:hover{color:var(--ink)}.ar{color:var(--brass)}
td{padding:6px 10px;border-bottom:1px solid rgba(40,52,64,.5);white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums}tbody tr:hover{background:var(--panel2)}
tr.sel{background:rgba(201,162,75,.12)}tr.sel:hover{background:rgba(201,162,75,.18)}
td.cb{width:34px;text-align:center}input[type=checkbox]{width:15px;height:15px;accent-color:var(--brass);cursor:pointer}
.idcell{font-family:ui-monospace,Menlo,monospace;font-size:.82rem}
.tag{font-size:.7rem;padding:1px 7px;border-radius:999px;border:1px solid currentColor;text-transform:capitalize}
.t-base{color:var(--base)}.t-instruct{color:var(--instruct)}.t-reasoning{color:var(--reasoning)}
.t-quant{color:var(--quant)}.t-finetune{color:var(--finetune)}
.lock{color:var(--warn)}.empty{padding:40px;text-align:center;color:var(--mut)}
::-webkit-scrollbar{width:11px;height:11px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:6px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--brass);color:#1a1206;padding:10px 18px;border-radius:8px;font-weight:600;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:1}
</style></head><body>
<aside>
<div class="brand"><div class="eb">ModelArk</div><h1>Build your set</h1></div>
<div class="budget">
<div><span class="big mono" id="selTB">0.00</span> <span class="of">/ <span id="bud">27</span> TB &middot; <span id="selN">0</span> models</span></div>
<div class="bar"><div id="barfill"></div></div><small id="bnote">raw full-precision</small>
</div>
<div class="tally" id="tally"></div>
<div class="acts"><button id="export">&#x2B07; Export set</button><button class="clear" id="clear">Clear</button></div>
</aside>
<main>
<div class="controls">
<div class="searchrow"><input id="search" placeholder="Search repo id or org…  (deepseek, qwen3-32b, -base, gguf)"><span class="shown" id="shown"></span></div>
<div class="filters" id="catf"></div><div class="filters" id="varf"></div><div class="filters" id="bucf"></div>
<div class="filters"><span class="flabel">flags</span>
<button class="chip toggle on" id="hideQuant">hide quant copies</button>
<button class="chip toggle" id="hideGated">hide gated</button>
<div class="bulk"><button id="selAll">&#x2713; select shown</button><button id="deselAll">&#x2717; deselect shown</button></div></div>
</div>
<div class="tablewrap"><table><thead><tr>
<th class="cb"></th><th data-k="id">Repo</th><th data-k="p" class="num">Params</th><th data-k="bucket">Bucket</th>
<th data-k="cat">Category</th><th data-k="v">Variant</th><th data-k="gb" class="num">Size</th>
<th data-k="dl" class="num">30d&nbsp;dl</th><th data-k="lic">License</th></tr></thead>
<tbody id="tbody"></tbody></table><div class="empty" id="empty" style="display:none">No models match these filters.</div></div>
</main><div class="toast" id="toast"></div>
<script>
const S={q:"",cat:new Set(),v:new Set(),bucket:new Set(),hide_quant:1,hide_gated:0,sort:"dl",dir:"desc"};
let BUD=27;
const api=(p,o)=>fetch(p,o).then(r=>r.json());
const gb=b=>b>=1e12?(b/1e12).toFixed(2)+"TB":(b/1e9).toFixed(0)+"GB";
const dl=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?Math.round(n/1e3)+"k":n;
function chip(t,cls){const b=document.createElement("button");b.className="chip "+(cls||"");b.textContent=t;return b;}
async function init(){
  const f=await api("/api/facets");BUD=f.budget;document.getElementById("bud").textContent=BUD;
  const cf=document.getElementById("catf");cf.innerHTML='<span class="flabel">category</span>';
  f.categories.forEach(c=>{const b=chip(c.name+" "+c.n);b.onclick=()=>{tog(S.cat,c.name,b);load();};cf.appendChild(b);});
  const vf=document.getElementById("varf");vf.innerHTML='<span class="flabel">variant</span>';
  f.variants.forEach(v=>{const b=chip(v,"v t-"+v);b.style.color="var(--"+v+")";
    b.onclick=()=>{tog(S.v,v,b);if(b.classList.contains("on"))b.style.background="var(--"+v+")";else b.style.background="";load();};vf.appendChild(b);});
  const bf=document.getElementById("bucf");bf.innerHTML='<span class="flabel">size</span>';
  f.buckets.forEach(bk=>{const b=chip(bk);b.onclick=()=>{tog(S.bucket,bk,b);load();};bf.appendChild(b);});
  load();refreshTally();
}
function tog(set,v,btn){set.has(v)?set.delete(v):set.add(v);btn.classList.toggle("on");}
function qs(){const p=new URLSearchParams();p.set("q",S.q);p.set("sort",S.sort);p.set("dir",S.dir);
  p.set("hide_quant",S.hide_quant);p.set("hide_gated",S.hide_gated);
  if(S.cat.size)p.set("cat",[...S.cat].join(","));if(S.v.size)p.set("v",[...S.v].join(","));
  if(S.bucket.size)p.set("bucket",[...S.bucket].join(","));return p.toString();}
async function load(){
  const d=await api("/api/models?"+qs());
  document.getElementById("tbody").innerHTML=d.rows.map(m=>`
    <tr class="${m.sel?'sel':''}" data-id="${m.id}">
    <td class="cb"><input type="checkbox" ${m.sel?'checked':''}></td>
    <td class="idcell">${m.id}${m.g?' <span class="lock" title="gated">&#128274;</span>':''}</td>
    <td class="num">${m.p!=null?m.p+'B':'—'}</td><td>${m.bucket}</td><td>${m.cat}</td>
    <td><span class="tag t-${m.v}">${m.v}</span></td><td class="num">${gb(m.bytes)}</td>
    <td class="num">${dl(m.dl)}</td><td>${m.lic}</td></tr>`).join("");
  document.getElementById("empty").style.display=d.rows.length?"none":"block";
  document.getElementById("shown").textContent=d.matched+" of "+d.total+(d.capped?" (showing "+d.rows.length+")":"")+" shown";
}
function renderTally(s){
  const tb=s.bytes/1e12;
  document.getElementById("selTB").textContent=tb.toFixed(2);
  document.getElementById("selN").textContent=s.n;
  const pct=Math.min(100,tb/BUD*100),bar=document.getElementById("barfill");
  bar.style.width=pct+"%";bar.style.background=tb>BUD?"var(--crit)":tb>BUD*.85?"var(--warn)":"var(--ok)";
  document.getElementById("bnote").textContent=tb>BUD?("over by "+(tb-BUD).toFixed(1)+" TB"):((BUD-tb).toFixed(1)+" TB left · ZipNN ~30% off bf16 on disk");
  document.getElementById("tally").innerHTML='<h2>your set · by category</h2>'+(s.by_cat.length?s.by_cat.map(g=>`
    <div class="crow"><div class="cc">${g.cat}</div><div class="cn">${g.n} · ${gb(g.bytes)}</div>
    ${g.recent?`<div class="rec">+ ${g.recent.split('/').pop()}</div>`:''}</div>`).join(""):
    '<div class="rec" style="padding:10px">Tick rows to build your set — it saves to the catalog as the wishlist.</div>');
}
const refreshTally=async()=>renderTally(await api("/api/selection"));
document.getElementById("tbody").addEventListener("change",async e=>{
  if(e.target.type!=="checkbox")return;const tr=e.target.closest("tr");
  tr.classList.toggle("sel",e.target.checked);
  renderTally(await api("/api/selection",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:tr.dataset.id,on:e.target.checked})}));
});
let dt;document.getElementById("search").addEventListener("input",e=>{S.q=e.target.value;clearTimeout(dt);dt=setTimeout(load,180);});
document.querySelectorAll("thead th[data-k]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;if(S.sort===k)S.dir=S.dir==="asc"?"desc":"asc";
  else{S.sort=k;S.dir=(k==="id"||k==="cat"||k==="v"||k==="lic"||k==="bucket")?"asc":"desc";}
  document.querySelectorAll("thead th .ar").forEach(a=>a.remove());
  th.insertAdjacentHTML("beforeend",' <span class="ar">'+(S.dir==="asc"?"▲":"▼")+'</span>');load();
});
document.getElementById("hideQuant").onclick=e=>{S.hide_quant^=1;e.target.classList.toggle("on");load();};
document.getElementById("hideGated").onclick=e=>{S.hide_gated^=1;e.target.classList.toggle("on");load();};
async function shownIds(){return (await api("/api/models?"+qs())).rows.map(m=>m.id);}
document.getElementById("selAll").onclick=async()=>{const ids=await shownIds();
  renderTally(await api("/api/selection/bulk",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids,on:true})}));load();toast(ids.length+" added");};
document.getElementById("deselAll").onclick=async()=>{const ids=await shownIds();
  renderTally(await api("/api/selection/bulk",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids,on:false})}));load();toast("deselected shown");};
document.getElementById("clear").onclick=async()=>{if(confirm("Clear the entire set?")){
  renderTally(await api("/api/selection/clear",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}));load();}};
document.getElementById("export").onclick=()=>{location="/api/export";toast("downloaded selection json");};
let tt;function toast(m){const t=document.getElementById("toast");t.textContent=m;t.classList.add("show");clearTimeout(tt);tt=setTimeout(()=>t.classList.remove("show"),1700);}
init();
</script></body></html>
"""
