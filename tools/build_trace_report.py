"""Build a standalone HTML report from Medium JSONL trace logs."""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append(
                    {
                        "_invalid": True,
                        "line_number": line_number,
                        "raw": line.rstrip(),
                    }
                )
    return records


def extract_question(traces: list[dict[str, Any]]) -> str | None:
    for trace in traces:
        if trace.get("stage") != "planning" or trace.get("event") != "output":
            continue
        try:
            payload = json.loads(trace.get("output", ""))
        except json.JSONDecodeError:
            continue
        question = payload.get("original_question")
        if isinstance(question, str):
            return question
    return None


def build_requests(
    timings: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    audits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    timing_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trace_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    audit_by_id: dict[str, dict[str, Any]] = {}
    for record in timings:
        if record.get("request_id"):
            timing_by_id[record["request_id"]].append(record)
    for record in traces:
        if record.get("request_id"):
            trace_by_id[record["request_id"]].append(record)
    for record in audits:
        if record.get("request_id"):
            audit_by_id[record["request_id"]] = record

    requests = []
    for request_id in set(timing_by_id) | set(trace_by_id) | set(audit_by_id):
        spans = sorted(timing_by_id[request_id], key=lambda item: item.get("started_at", ""))
        request_traces = sorted(trace_by_id[request_id], key=lambda item: item.get("timestamp", ""))
        audit = audit_by_id.get(request_id, {})
        total = next((span for span in spans if span.get("stage") == "request_total"), None)
        started_at = min(
            (span.get("started_at") for span in spans if span.get("started_at")),
            default=audit.get("timestamp", ""),
        )
        requests.append(
            {
                "request_id": request_id,
                "question": extract_question(request_traces),
                "question_sha256": audit.get("question_sha256"),
                "started_at": started_at,
                "status": audit.get("status") or (total or {}).get("status") or "unknown",
                "duration_ms": audit.get("duration_ms") or (total or {}).get("duration_ms") or 0,
                "error_code": audit.get("error_code"),
                "planning_mode": audit.get("planning_mode"),
                "query_plan": audit.get("query_plan"),
                "row_count": audit.get("row_count"),
                "result_sets": audit.get("result_sets"),
                "tables": audit.get("tables", []),
                "spans": [span for span in spans if span.get("stage") != "request_total"],
                "traces": request_traces,
                "result_snapshot": None,
            }
        )
    return sorted(requests, key=lambda item: item["started_at"], reverse=True)


def render_report(requests: list[dict[str, Any]]) -> str:
    data = json.dumps(requests, ensure_ascii=False).replace("</", "<\\/")
    generated = html.escape(__import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Medium 调用链观测</title>
<style>
:root {{ color-scheme: light; --bg:#f5f6f8; --panel:#fff; --text:#1f2933; --muted:#667085; --line:#d9dee7; --ok:#18864b; --bad:#c23b32; --active:#1668dc; --llm:#7c4dba; --local:#168a8a; }}
* {{ box-sizing:border-box }} body {{ margin:0; font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif; color:var(--text); background:var(--bg) }}
button,input,select {{ font:inherit }} button {{ cursor:pointer }}
.shell {{ min-height:100vh; display:grid; grid-template-columns:minmax(280px,360px) 1fr }}
.sidebar {{ background:var(--panel); border-right:1px solid var(--line); padding:18px; min-width:0 }}
.main {{ padding:22px; min-width:0 }} h1,h2,h3,p {{ margin-top:0 }} h1 {{ font-size:20px; margin-bottom:4px }} h2 {{ font-size:18px }}
.muted {{ color:var(--muted) }} .toolbar {{ display:grid; grid-template-columns:1fr 112px; gap:8px; margin:18px 0 12px }}
input,select {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:8px 10px; background:var(--panel); color:var(--text) }}
.requests {{ display:grid; gap:6px; max-height:calc(100vh - 150px); overflow:auto }}
.request {{ width:100%; text-align:left; border:1px solid transparent; border-radius:6px; background:transparent; padding:10px }}
.request:hover,.request.active {{ background:#eef4ff; border-color:#b8d2fa }} .request-top {{ display:flex; justify-content:space-between; gap:10px }}
.question {{ margin-top:5px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden }}
.status {{ font-weight:600 }} .success {{ color:var(--ok) }} .failed,.error {{ color:var(--bad) }}
.summary {{ display:grid; grid-template-columns:repeat(4,minmax(130px,1fr)); gap:10px; margin-bottom:22px }}
.metric {{ border:1px solid var(--line); border-radius:6px; background:var(--panel); padding:12px }} .metric strong {{ display:block; font-size:19px; margin-top:3px }}
.section {{ margin:0 0 24px }} .timeline {{ position:relative; border-top:1px solid var(--line); padding-top:14px }}
.lane {{ display:grid; grid-template-columns:190px 1fr 72px; gap:10px; align-items:center; min-height:34px }}
.bar-area {{ position:relative; height:20px; background:#e9edf3; border-radius:3px }} .bar {{ position:absolute; min-width:2px; height:100%; border-radius:3px; background:var(--llm) }} .bar.local {{ background:var(--local) }}
.stage-button {{ border:0; background:transparent; padding:3px 0; text-align:left; color:inherit }} .stage-button:hover {{ color:var(--active) }}
.detail {{ border-top:1px solid var(--line); padding-top:16px }} .detail-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-bottom:12px }}
.detail-grid div {{ background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:9px }}
pre {{ margin:8px 0 14px; padding:12px; border:1px solid var(--line); border-radius:6px; background:#111827; color:#e5e7eb; overflow:auto; max-height:360px; white-space:pre-wrap; word-break:break-word }}
.notice {{ border-left:3px solid #d99a1c; background:#fff7df; padding:10px 12px; margin:10px 0 }} .empty {{ padding:40px; text-align:center; color:var(--muted) }}
@media (max-width:900px) {{ .shell {{ grid-template-columns:1fr }} .sidebar {{ border-right:0; border-bottom:1px solid var(--line) }} .requests {{ max-height:280px }} .summary {{ grid-template-columns:repeat(2,1fr) }} .lane {{ grid-template-columns:140px 1fr 62px }} }}
@media (max-width:520px) {{ .main {{ padding:14px }} .summary,.detail-grid {{ grid-template-columns:1fr }} .lane {{ grid-template-columns:110px 1fr 56px }} }}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <h1>Medium 调用链</h1><p class="muted">生成于 {generated}</p>
    <div class="toolbar"><input id="search" placeholder="搜索问题或请求 ID"><select id="status"><option value="all">全部状态</option><option value="success">成功</option><option value="failed">失败</option></select></div>
    <div id="requests" class="requests"></div>
  </aside>
  <main id="main" class="main"><div class="empty">选择一次请求查看完整链条</div></main>
</div>
<script>
const DATA={data};
const $=s=>document.querySelector(s); let selected=null;
const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const fmt=ms=>ms>=1000?(ms/1000).toFixed(2)+' 秒':ms+' ms';
const label=s=>({{routing:'路由',planning:'规划',planning_deterministic_route:'确定性规划',sql_generation_initial:'SQL 生成',sql_guard_initial:'SQL Guard',pre_execution_review_1:'执行前审核 1',sql_semantic_revision_1:'SQL 修订',sql_guard_revision_1:'修订后 Guard',pre_execution_review_2:'执行前审核 2',execution_primary:'数据库执行',result_review_1:'结果审核'}}[s]||s);
const isLocal=s=>s.includes('guard')||s.includes('execution')||s==='routing';
function renderList() {{
 const q=$('#search').value.trim().toLowerCase(), status=$('#status').value;
 const rows=DATA.filter(r=>(status==='all'||r.status===status)&&(!q||(r.question||'').toLowerCase().includes(q)||r.request_id.toLowerCase().includes(q)));
 $('#requests').innerHTML=rows.map(r=>`<button class="request ${{selected===r.request_id?'active':''}}" data-id="${{esc(r.request_id)}}"><div class="request-top"><span class="status ${{esc(r.status)}}">${{r.status==='success'?'成功':'失败'}}</span><strong>${{fmt(r.duration_ms)}}</strong></div><div class="question">${{esc(r.question||r.request_id)}}</div><small class="muted">${{esc(r.started_at)}}</small></button>`).join('')||'<div class="empty">没有匹配请求</div>';
 document.querySelectorAll('.request').forEach(b=>b.onclick=()=>show(b.dataset.id));
}}
function show(id) {{
 selected=id; renderList(); const r=DATA.find(x=>x.request_id===id); if(!r)return;
 const starts=r.spans.map(s=>Date.parse(s.started_at)).filter(Number.isFinite); const base=Math.min(...starts); const end=Math.max(...r.spans.map(s=>Date.parse(s.started_at)+s.duration_ms)); const range=Math.max(1,end-base);
 const bars=r.spans.map((s,i)=>{{const left=(Date.parse(s.started_at)-base)/range*100,width=Math.max(.4,s.duration_ms/range*100);return `<div class="lane"><button class="stage-button" data-stage="${{i}}">${{esc(label(s.stage))}}</button><div class="bar-area"><span class="bar ${{isLocal(s.stage)?'local':''}}" style="left:${{left}}%;width:${{width}}%"></span></div><span>${{fmt(s.duration_ms)}}</span></div>`}}).join('');
 const llm=r.spans.filter(s=>!isLocal(s.stage)).reduce((a,s)=>a+s.duration_ms,0); const slow=[...r.spans].sort((a,b)=>b.duration_ms-a.duration_ms)[0];
 $('#main').innerHTML=`<h2>${{esc(r.question||'请求详情')}}</h2><p class="muted"><code>${{esc(r.request_id)}}</code></p><div class="summary"><div class="metric"><span class="muted">总耗时</span><strong>${{fmt(r.duration_ms)}}</strong></div><div class="metric"><span class="muted">状态</span><strong class="${{esc(r.status)}}">${{r.status==='success'?'成功':'失败'}}</strong></div><div class="metric"><span class="muted">LLM 阶段</span><strong>${{fmt(llm)}}</strong></div><div class="metric"><span class="muted">最慢阶段</span><strong>${{slow?esc(label(slow.stage)):'-'}}</strong></div></div><section class="section"><h3>阶段瀑布</h3><div class="timeline">${{bars}}</div></section><section class="detail"><h3>阶段详情</h3><div id="stage-detail" class="muted">点击阶段名称查看输入输出</div></section><section class="section"><h3>执行结果</h3>${{r.result_snapshot?`<pre>${{esc(JSON.stringify(r.result_snapshot,null,2))}}</pre>`:`<div class="notice">当前审计日志仅记录 ${{r.row_count??'未知'}} 行、${{r.result_sets??'未知'}} 个结果集，尚未采集查询结果正文。</div>`}}<pre>${{esc(JSON.stringify({{status:r.status,error_code:r.error_code,planning_mode:r.planning_mode,query_plan:r.query_plan,tables:r.tables,row_count:r.row_count,result_sets:r.result_sets}},null,2))}}</pre></section>`;
 document.querySelectorAll('.stage-button').forEach(b=>b.onclick=()=>showStage(r,Number(b.dataset.stage)));
 if(r.spans.length)showStage(r,0);
}}
function showStage(r,index) {{ const s=r.spans[index], traces=r.traces.filter(t=>t.stage===s.stage); $('#stage-detail').innerHTML=`<div class="detail-grid"><div><span class="muted">状态</span><br><strong class="${{esc(s.status)}}">${{esc(s.status)}}</strong></div><div><span class="muted">耗时</span><br><strong>${{fmt(s.duration_ms)}}</strong></div><div><span class="muted">开始时间</span><br><strong>${{esc(s.started_at||'-')}}</strong></div></div>${{traces.length?traces.map(t=>`<p><strong>${{esc(t.event==='output'?'模型输出':'模型错误')}}</strong> <span class="muted">${{esc(t.provider||'')}} / ${{esc(t.model||'')}}</span></p><pre>${{esc(t.output||t.error_type||'')}}</pre>`).join(''):'<div class="notice">该阶段没有模型输出；它可能是本地路由、Guard 或数据库执行阶段。</div>'}}`; }}
$('#search').addEventListener('input',renderList); $('#status').addEventListener('change',renderList); renderList(); if(DATA.length)show(DATA[0].request_id);
</script>
</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=BASE_DIR / "runtime")
    parser.add_argument("--output", type=Path, default=BASE_DIR / "runtime" / "trace-report.html")
    args = parser.parse_args()
    requests = build_requests(
        read_jsonl(args.runtime_dir / "stage_timing.jsonl"),
        read_jsonl(args.runtime_dir / "llm_trace.jsonl"),
        read_jsonl(args.runtime_dir / "query_audit.jsonl"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_report(requests), encoding="utf-8")
    print(f"Wrote {len(requests)} requests to {args.output}")


if __name__ == "__main__":
    main()
