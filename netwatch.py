#!/usr/bin/env python3
"""netwatch - a tiny, zero-dependency internet uptime monitor.

Every INTERVAL seconds it opens a single TCP connection to a public anycast IP
(no DNS, no HTTP payload, ~3 packets) and stores ok/latency in SQLite.
A built-in web UI shows the history. Python stdlib only.
"""
import json
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__version__ = os.environ.get("NETWATCH_VERSION", "dev")

DB = os.environ.get("NETWATCH_DB", "./data/netwatch.db")
BIND = os.environ.get("NETWATCH_BIND", "0.0.0.0")
PORT = int(os.environ.get("NETWATCH_PORT", "8080"))
INTERVAL = int(os.environ.get("NETWATCH_INTERVAL", "60"))
RETENTION_DAYS = int(os.environ.get("NETWATCH_RETENTION_DAYS", "365"))
TIMEOUT = float(os.environ.get("NETWATCH_TIMEOUT", "3"))
# Tried in order; the first one that accepts a TCP connection counts as "online".
TARGETS = []
for _t in os.environ.get("NETWATCH_TARGETS", "1.1.1.1,8.8.8.8,9.9.9.9").split(","):
    _t = _t.strip()
    if not _t:
        continue
    _host, _, _port = _t.rpartition(":") if _t.count(":") == 1 else (_t, "", "")
    TARGETS.append((_host, int(_port)) if _port else (_t, 443))

# range -> (span seconds, bucket seconds)
RANGES = {
    "24h": (86400, 900),         # 96 x 15 min
    "7d": (7 * 86400, 3600),     # 168 x 1 h
    "30d": (30 * 86400, 21600),  # 120 x 6 h
    "90d": (90 * 86400, 86400),  # 90 x 1 day
}


def db():
    return sqlite3.connect(DB, timeout=10)


def init_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = db()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS checks ("
        " ts INTEGER PRIMARY KEY, ok INTEGER NOT NULL, ms REAL)"
    )
    c.commit()
    c.close()


def check():
    for host, port in TARGETS:
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT):
                return True, round((time.monotonic() - t0) * 1000, 1)
        except OSError:
            continue
    return False, None


def checker():
    conn = db()
    last_prune = 0
    while True:
        now = time.time()
        slot = (int(now) // INTERVAL + 1) * INTERVAL
        time.sleep(max(0, slot - now))
        ok, ms = check()
        try:
            conn.execute("INSERT OR REPLACE INTO checks VALUES (?,?,?)", (slot, int(ok), ms))
            if slot - last_prune > 86400:
                conn.execute("DELETE FROM checks WHERE ts < ?", (slot - RETENTION_DAYS * 86400,))
                last_prune = slot
            conn.commit()
        except sqlite3.Error as e:
            print(f"db error: {e}", flush=True)


def build(rng):
    span, bucket = RANGES[rng]
    now = int(time.time())
    end = (now // bucket + 1) * bucket
    start = end - span
    conn = db()
    try:
        buckets = [None] * (span // bucket)
        for b, n, up, ms in conn.execute(
            "SELECT (ts - ?) / ? AS b, COUNT(*), SUM(ok), AVG(CASE WHEN ok THEN ms END)"
            " FROM checks WHERE ts >= ? AND ts < ? GROUP BY b",
            (start, bucket, start, end),
        ):
            buckets[b] = [n, up, round(ms, 1) if ms is not None else None]

        stats = {}
        for k, (s, _) in RANGES.items():
            n, up, ms = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(ok), 0), AVG(CASE WHEN ok THEN ms END)"
                " FROM checks WHERE ts >= ?",
                (now - s,),
            ).fetchone()
            stats[k] = {"n": n, "up": up, "ms": round(ms, 1) if ms is not None else None}

        last = conn.execute("SELECT ts, ok, ms FROM checks ORDER BY ts DESC LIMIT 1").fetchone()
        first = conn.execute("SELECT MIN(ts) FROM checks").fetchone()[0]

        # Consecutive failed checks (gap <= 1.5 intervals) form one outage.
        runs = []
        for (ts,) in conn.execute(
            "SELECT ts FROM checks WHERE ok = 0 AND ts >= ? ORDER BY ts", (start,)
        ):
            if runs and ts - runs[-1][1] <= INTERVAL * 1.5:
                runs[-1][1] = ts
            else:
                runs.append([ts, ts])
        outages = [
            {
                "start": a,
                "end": b + INTERVAL,
                "ongoing": bool(last and not last[1] and last[0] == b),
            }
            for a, b in runs
        ]
        outages.reverse()
    finally:
        conn.close()

    return {
        "range": rng,
        "start": start,
        "end": end,
        "bucket": bucket,
        "interval": INTERVAL,
        "buckets": buckets,
        "stats": stats,
        "last": {"ts": last[0], "ok": bool(last[1]), "ms": last[2]} if last else None,
        "first": first,
        "outages": outages[:200],
        "version": __version__,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE)
        elif u.path == "/api/data":
            rng = parse_qs(u.query).get("range", ["24h"])[0]
            if rng not in RANGES:
                rng = "24h"
            self._send(200, "application/json", json.dumps(build(rng)).encode())
        elif u.path == "/healthz":
            self._send(200, "text/plain", b"ok")
        else:
            self._send(404, "text/plain", b"not found")

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>netwatch</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6' fill='%232f9e5f'/></svg>">
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1c2126;--mut:#6b7480;--bd:#e2e5e9;
 --up:#2f9e5f;--down:#d8443a;--part:#e3a03a;--none:#d7dbe0}
@media (prefers-color-scheme:dark){:root{--bg:#111316;--card:#1a1d21;--fg:#e6e8eb;
 --mut:#8b939d;--bd:#2a2e33;--none:#2d3238}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 32px}
header{display:flex;align-items:center;gap:14px;margin-bottom:20px}
.dot{width:16px;height:16px;border-radius:50%;background:var(--none);flex:none}
.dot.up{background:var(--up);box-shadow:0 0 0 5px color-mix(in srgb,var(--up) 22%,transparent)}
.dot.down{background:var(--down);box-shadow:0 0 0 5px color-mix(in srgb,var(--down) 22%,transparent)}
h1{font-size:20px;margin:0}
.sub{color:var(--mut);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px}
.card .l{color:var(--mut);font-size:13px}
.card .v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.card .s{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
section{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:20px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
h2{font-size:15px;margin:0}
.tabs{display:flex;gap:4px}
.tabs button{border:1px solid var(--bd);background:transparent;color:var(--fg);border-radius:6px;padding:4px 10px;font:inherit;font-size:13px;cursor:pointer}
.tabs button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.bars{display:flex;gap:1px;height:56px}
.bars div{flex:1;min-width:1px;border-radius:2px;background:var(--none)}
.bars div.up{background:var(--up)}.bars div.down{background:var(--down)}.bars div.part{background:var(--part)}
.bars div:hover{opacity:.7}
.axis{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;margin-top:6px}
.legend{display:flex;gap:14px;color:var(--mut);font-size:12px;margin-top:10px;flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
#tip{color:var(--mut);font-size:13px;min-height:20px;margin-top:8px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--bd);font-size:14px}
th{color:var(--mut);font-weight:500;font-size:12px}
.empty{color:var(--mut);padding:8px 0}
.live{color:var(--down);font-weight:600}
footer{text-align:center;color:var(--mut);font-size:12px}
footer a{color:inherit}
</style>
</head>
<body>
<main>
<header><div id="dot" class="dot"></div><div><h1 id="status"></h1><div class="sub" id="substatus"></div></div></header>
<div class="cards" id="cards"></div>
<section>
 <div class="row"><h2 data-t="history"></h2><div class="tabs" id="tabs"></div></div>
 <div class="bars" id="bars"></div>
 <div class="axis"><span id="ax0"></span><span data-t="now"></span></div>
 <div id="tip"></div>
 <div class="legend"><span><i style="background:var(--up)"></i><span data-t="lgUp"></span></span><span><i style="background:var(--part)"></i><span data-t="lgPart"></span></span><span><i style="background:var(--down)"></i><span data-t="lgDown"></span></span><span><i style="background:var(--none)"></i><span data-t="lgNone"></span></span></div>
</section>
<section>
 <div class="row"><h2 data-t="outages"></h2><span class="sub" id="ocount"></span></div>
 <div id="outages"></div>
</section>
<footer>netwatch <span id="ver"></span></footer>
</main>
<script>
const I18N={
 en:{loading:"Loading…",noData:"No data yet",firstSoon:"The first check runs within a minute.",stale:"Checks seem to have stopped",
  online:"Online",offline:"Offline",lastCheck:"Last check",since:"recording since",uptime:"Uptime",failed:"failed checks",
  noOutage:"no outages",avg:"avg",nodata:"no data",ok:"successful",downMin:"min down",history:"History",now:"now",
  lgUp:"up",lgPart:"partial outage",lgDown:"down",lgNone:"no data (monitor off)",outages:"Outages",outCount:"outages",outCount1:"outage",
  noOutages:"No outages in this range.",start:"Start",end:"End",duration:"Duration",ongoing:"ongoing",unreachable:"Monitor unreachable",
  sAgo:"s ago",mAgo:"min ago",m:"min",h:"h",d:"d"},
 tr:{loading:"Yükleniyor…",noData:"Henüz veri yok",firstSoon:"İlk kontrol bir dakika içinde yapılır.",stale:"Kontrol durmuş görünüyor",
  online:"İnternet var",offline:"İnternet yok",lastCheck:"Son kontrol",since:"kayıt başlangıcı",uptime:"Uptime",failed:"başarısız kontrol",
  noOutage:"kesinti yok",avg:"ort",nodata:"veri yok",ok:"başarılı",downMin:"dk kesinti",history:"Geçmiş",now:"şimdi",
  lgUp:"sorunsuz",lgPart:"kısmi kesinti",lgDown:"kesinti",lgNone:"veri yok (izleyici kapalı)",outages:"Kesintiler",outCount:"kesinti",outCount1:"kesinti",
  noOutages:"Bu aralıkta kesinti yok.",start:"Başlangıç",end:"Bitiş",duration:"Süre",ongoing:"devam ediyor",unreachable:"İzleyiciye ulaşılamıyor",
  sAgo:"sn önce",mAgo:"dk önce",m:"dk",h:"sa",d:"g"}};
const LANG=(navigator.language||"en").slice(0,2) in I18N?(navigator.language||"en").slice(0,2):"en";
const T=I18N[LANG];document.documentElement.lang=LANG;
document.querySelectorAll("[data-t]").forEach(e=>e.textContent=T[e.dataset.t]);
const RANGES=["24h","7d","30d","90d"];
let range="24h";
try{range=localStorage.getItem("nw-range")||"24h"}catch(e){}
const qr=new URLSearchParams(location.search).get("range");if(qr)range=qr;
if(!RANGES.includes(range))range="24h";
const $=id=>document.getElementById(id);
$("status").textContent=T.loading;
const fmt=(ts,o)=>new Date(ts*1000).toLocaleString(undefined,o||{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
const pct=s=>s.n?(100*s.up/s.n).toFixed(s.up===s.n?0:2)+"%":"—";
function dur(sec){const m=Math.max(1,Math.round(sec/60));if(m<60)return m+" "+T.m;const h=Math.floor(m/60);if(h<24)return h+" "+T.h+(m%60?" "+m%60+" "+T.m:"");return Math.floor(h/24)+" "+T.d+" "+(h%24)+" "+T.h}
function ago(ts){const s=Math.max(0,Math.round(Date.now()/1000-ts));return s<90?s+" "+T.sAgo:Math.round(s/60)+" "+T.mAgo}

function tabs(){$("tabs").innerHTML="";for(const r of RANGES){const b=document.createElement("button");b.textContent=r;if(r===range)b.className="on";b.onclick=()=>{range=r;try{localStorage.setItem("nw-range",r)}catch(e){}tabs();load()};$("tabs").appendChild(b)}}

function render(d){
 $("ver").textContent=d.version;
 const L=d.last;
 if(!L){$("status").textContent=T.noData;$("substatus").textContent=T.firstSoon;}
 else{
  const stale=Date.now()/1000-L.ts>d.interval*3;
  $("dot").className="dot "+(stale?"":L.ok?"up":"down");
  $("status").textContent=stale?T.stale:L.ok?T.online:T.offline;
  $("substatus").textContent=T.lastCheck+" "+ago(L.ts)+(L.ok&&L.ms!=null?" · "+L.ms+" ms":"")+(d.first?" · "+T.since+" "+fmt(d.first,{day:"2-digit",month:"short",year:"numeric"}):"");
 }
 $("cards").innerHTML="";
 for(const k of RANGES){const s=d.stats[k];const c=document.createElement("div");c.className="card";
  const down=s.n-s.up;
  c.innerHTML='<div class="l">'+T.uptime+' · '+k+'</div><div class="v">'+pct(s)+'</div><div class="s">'+(s.n?(down?down+" "+T.failed:T.noOutage)+(s.ms!=null?" · "+T.avg+" "+s.ms+" ms":""):T.nodata)+'</div>';
  $("cards").appendChild(c)}
 const bars=$("bars");bars.innerHTML="";
 d.buckets.forEach((b,i)=>{const el=document.createElement("div");const t0=d.start+i*d.bucket,t1=t0+d.bucket;
  let txt=fmt(t0)+" – "+fmt(t1,{hour:"2-digit",minute:"2-digit"})+": ";
  if(!b){txt+=T.nodata}else{const[n,up,ms]=b;el.className=up===n?"up":up===0?"down":"part";
   txt+=up+"/"+n+" "+T.ok+(n-up?" ("+Math.round((n-up)*d.interval/60)+" "+T.downMin+")":"")+(ms!=null?" · "+T.avg+" "+ms+" ms":"")}
  el.title=txt;el.onmouseenter=()=>$("tip").textContent=txt;bars.appendChild(el)});
 $("ax0").textContent=fmt(d.start);$("tip").textContent="";
 const o=d.outages;$("ocount").textContent=o.length?o.length+" "+(o.length===1?T.outCount1:T.outCount)+" ("+range+")":"";
 if(!o.length){$("outages").innerHTML='<div class="empty">'+T.noOutages+'</div>';return}
 let h="<table><tr><th>"+T.start+"</th><th>"+T.end+"</th><th>"+T.duration+"</th></tr>";
 for(const x of o){h+="<tr><td>"+fmt(x.start)+"</td><td>"+(x.ongoing?'<span class="live">'+T.ongoing+'</span>':fmt(x.end))+"</td><td>"+(x.ongoing?dur(Date.now()/1000-x.start):dur(x.end-x.start))+"</td></tr>"}
 $("outages").innerHTML=h+"</table>";
}
async function load(){try{const r=await fetch("api/data?range="+range);render(await r.json())}catch(e){$("status").textContent=T.unreachable;$("dot").className="dot"}}
tabs();load();setInterval(load,60000);
</script>
</body>
</html>
""".encode()


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    init_db()
    threading.Thread(target=checker, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    targets = ", ".join(f"{h}:{p}" for h, p in TARGETS)
    print(
        f"netwatch {__version__} listening on http://{BIND}:{PORT} "
        f"(db={DB}, interval={INTERVAL}s, targets={targets})",
        flush=True,
    )
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
