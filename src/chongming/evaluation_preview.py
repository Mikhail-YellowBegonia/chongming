"""Build a self-contained, sampled visual audit of an existing device evaluation."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

import cv2

from chongming.evaluation import annotate


def select_frames(records: list[dict[str, Any]], per_group: int) -> list[dict[str, Any]]:
    """Include worst misses, worst false positives and uniform temporal context."""
    chosen: dict[str, dict[str, Any]] = {}
    for station in sorted({r["station_id"] for r in records}):
        rows = [r for r in records if r["station_id"] == station]
        for key in ("fn", "fp"):
            for row in sorted(rows, key=lambda r: (-r[key], r["time_ns"]))[:per_group]:
                chosen[row["frame_id"]] = row
        for i in range(min(per_group, len(rows))):
            row = rows[round(i * (len(rows) - 1) / max(1, per_group - 1))]
            chosen[row["frame_id"]] = row
    return sorted(chosen.values(), key=lambda r: (r["station_id"], r["time_ns"]))


def build(evaluation: Path, output: Path, per_group: int = 8) -> int:
    if per_group < 1:
        raise ValueError("per_group must be positive")
    if output.exists():
        raise FileExistsError(output)
    summary = json.loads((evaluation / "summary.json").read_text())
    if summary["status"] != "complete" or summary["format"] != "device-evaluation-v1":
        raise ValueError("a complete device evaluation is required")
    recording = Path(summary["recording"])
    records = [
        r
        for line in (evaluation / "frames.jsonl").read_text().splitlines()
        if not (r := json.loads(line))["warmup_excluded"]
    ]
    selected = select_frames(records, per_group)
    by_id = {r["frame_id"]: r for r in selected}
    metadata = {}
    for line in (recording / "online.jsonl").read_text().splitlines():
        for frame in json.loads(line)["frames"]:
            if frame["frame_id"] in by_id:
                metadata[frame["frame_id"]] = frame
    if set(metadata) != set(by_id):
        raise ValueError("preview frames missing from recording")
    gate = summary["aggregate"]["gate"]
    for path in sorted({m["path"] for m in metadata.values()}):
        source = (recording / path).resolve()
        if not source.is_relative_to(recording.resolve()):
            raise ValueError("video path escapes recording")
        capture = cv2.VideoCapture(str(source))
        wanted = {m["frame_index"]: by_id[key] for key, m in metadata.items() if m["path"] == path}
        try:
            for index in range(max(wanted) + 1):
                ok, frame = capture.read()
                if not ok:
                    raise ValueError("recorded video ended before selected frame")
                if index in wanted:
                    row = wanted[index]
                    row.update(annotate(row, gate))
                    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    if not ok:
                        raise RuntimeError("JPEG encoding failed")
                    row["image"] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        finally:
            capture.release()
    payload = {"frames": selected, "summary": summary["aggregate"], "gate": gate}
    encoded = json.dumps(payload, allow_nan=False).replace("<", "\\u003c")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        handle.write(HTML.replace("__PAYLOAD__", encoded))
    return len(selected)


HTML = r"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>设备评分可视化复查</title><style>
body{font:15px system-ui;background:#151b24;color:#e8edf5;margin:24px}button,select,input{margin:6px;padding:7px}canvas{background:#05080c;max-width:100%;border:1px solid #657080}#layout{display:flex;gap:20px;flex-wrap:wrap}table{border-collapse:collapse}td,th{padding:6px 14px;border-bottom:1px solid #465060}p{max-width:1100px}#detail{white-space:pre-wrap}a{color:#8fd4ff}
</style><h1>设备评分可视化复查</h1>
<p>这是已保存评分的抽样复查，不重新运行算法。每站选取漏检最多、误报最多的帧及均匀时间样本；不能用这些偏向困难帧的样本重新估算总体指标。预热帧已排除。</p>
<div id="overall"></div><details><summary>全部评价帧的逐目标召回率</summary><table id="ranking"></table></details>
<p>绿色圆圈＝匹配目标；红色圆圈＝漏检责任目标；青色十字＝已匹配检测；橙色十字＝误报；紫色十字＝重复检测。圆圈半径为各目标的实际匹配门限。点击原图可放大局部。</p>
<select id="station"></select><select id="filter"><option value="all">全部样本</option><option value="fn">有漏检</option><option value="fp">有误报</option></select>
<button id="prev">上一帧</button><button id="next">下一帧</button><label><input type="checkbox" id="overlay" checked>评分叠加</label>
<input id="slider" type="range" min="0" value="0"><div id="detail"></div>
<div id="layout"><canvas id="scene"></canvas><div><canvas id="zoom" width="400" height="400"></canvas><p>局部放大：原图 100×100 像素，放大 4 倍</p><div id="targets"></div></div></div>
<p>漏检表示本帧没有门限内的一对一实测输出；无法仅凭此报告断定具体算法阶段。误差越界通常表现为相邻的红圈与橙色十字。植物等背景附近的误报、细小目标或合并目标需结合原图检查。遮挡未记录，不能据此确认目标未被遮挡。</p>
<script>const D=__PAYLOAD__;
const $=id=>document.getElementById(id), pct=x=>x===null?'—':(100*x).toFixed(2)+'%';
const s=D.summary;$('overall').textContent=`全量结果：召回率 ${pct(s.recall)}；精确率 ${pct(s.precision)}；TP ${s.tp} / FP ${s.fp} / FN ${s.fn}；像素门限下限 ${D.gate} px（实际门限见圆圈）`;
for(const [id,v] of Object.entries(s.per_target).sort((a,b)=>a[1].recall-b[1].recall)){const tr=$('ranking').insertRow();for(const text of [id,`召回 ${pct(v.recall)}`,`TP ${v.tp}`,`FN ${v.fn}`])tr.insertCell().textContent=text;}
for(const station of [...new Set(D.frames.map(f=>f.station_id))]){const o=document.createElement('option');o.value=station;o.textContent=station;$('station').append(o);}
let frames=[],index=0,current=null,img=new Image(),cx=320,cy=240,token=0;
const canvas=$('scene'),ctx=canvas.getContext('2d'),z=$('zoom').getContext('2d');
function crop(){z.imageSmoothingEnabled=false;z.clearRect(0,0,400,400);z.drawImage(canvas,cx-50,cy-50,100,100,0,0,400,400);}
function paint(){ctx.drawImage(img,0,0);if($('overlay').checked){ctx.font='11px monospace';ctx.lineWidth=1.5;current.targets.forEach(t=>{const [x,y]=t.xy;ctx.strokeStyle=t.matched?'#45ff91':'#ff5151';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.arc(x,y,t.gate,0,Math.PI*2);ctx.stroke();ctx.fillText(t.id,x+12,y-8);});current.predictions.forEach(p=>{const [x,y]=p.xy;ctx.strokeStyle=p.kind==='matched'?'#39e5ff':p.kind==='duplicate'?'#dc8bff':'#ffb23f';ctx.beginPath();ctx.moveTo(x-5,y);ctx.lineTo(x+5,y);ctx.moveTo(x,y-5);ctx.lineTo(x,y+5);if(p.target>=0){ctx.moveTo(x,y);ctx.lineTo(...current.targets[p.target].xy);}ctx.stroke();});}crop();}
function show(){if(!frames.length){$('detail').textContent='无对应样本';return;}index=Math.max(0,Math.min(index,frames.length-1));$('slider').value=index;current=frames[index];$('detail').textContent=`样本 ${index+1}/${frames.length} · ${current.frame_id}\nTP ${current.tp} / FP ${current.fp} / FN ${current.fn} · 召回 ${pct(current.recall)} · 精确率 ${pct(current.precision)}`;
$('targets').replaceChildren();for(const t of current.targets){const b=document.createElement('button');b.textContent=(t.matched?'匹配 ':'漏检 ')+t.id+` (${t.gate.toFixed(1)}px)`;b.onclick=()=>{[cx,cy]=t.xy;crop();};$('targets').append(b);}
const n=++token;const next=new Image();next.onload=()=>{if(n!==token)return;img=next;canvas.width=img.width;canvas.height=img.height;const missed=current.targets.find(t=>!t.matched)||current.targets[0];if(missed)[cx,cy]=missed.xy;paint();};next.src=current.image;}
function reset(){frames=D.frames.filter(f=>f.station_id===$('station').value&&($('filter').value==='all'||f[$('filter').value]>0));index=0;$('slider').max=Math.max(0,frames.length-1);show();}
$('station').onchange=reset;$('filter').onchange=reset;$('prev').onclick=()=>{index--;show();};$('next').onclick=()=>{index++;show();};$('slider').oninput=()=>{index=Number($('slider').value);show();};$('overlay').onchange=()=>{if(current)paint();};canvas.onclick=e=>{const r=canvas.getBoundingClientRect();cx=(e.clientX-r.left)*canvas.width/r.width;cy=(e.clientY-r.top)*canvas.height/r.height;crop();};document.onkeydown=e=>{if(e.key==='ArrowRight')$('next').click();if(e.key==='ArrowLeft')$('prev').click();};reset();
</script></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-group", type=int, default=8)
    args = parser.parse_args()
    count = build(args.evaluation, args.output, args.per_group)
    print(f"Wrote {count} sampled frames to {args.output}")


if __name__ == "__main__":
    main()
