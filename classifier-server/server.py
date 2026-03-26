"""Classifier server: receives JPEG images, runs YOLOv8, serves a detection gallery."""

import io
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from PIL import Image
from ultralytics import YOLO

DETECTIONS_DIR = Path(__file__).parent / "detections"
DETECTIONS_DIR.mkdir(exist_ok=True)

MODEL_NAME = "yolov8n.pt"
CONFIDENCE_THRESHOLD = 0.35

ANIMAL_CLASSES = {
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe",
}

app = FastAPI(title="Backyard Classifier")
model = None


def get_model():
    global model
    if model is None:
        model = YOLO(MODEL_NAME)
    return model


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/classify")
async def classify(request: Request):
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty body"}, status_code=400)

    img = Image.open(io.BytesIO(body)).convert("RGB")
    m = get_model()
    results = m(img, conf=CONFIDENCE_THRESHOLD, verbose=False)

    detections = []
    for r in results:
        for box in r.boxes:
            cls_id = int(box.cls[0])
            cls_name = r.names[cls_id]
            conf = float(box.conf[0])
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            detections.append({
                "class": cls_name,
                "confidence": round(conf, 3),
                "bbox": [round(x1), round(y1), round(x2), round(y2)],
            })

    detection_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    det_dir = DETECTIONS_DIR / detection_id
    det_dir.mkdir(parents=True, exist_ok=True)

    orig_path = det_dir / "original.jpg"
    orig_path.write_bytes(body)

    annotated = results[0].plot()
    ann_img = Image.fromarray(annotated[..., ::-1])
    ann_path = det_dir / "annotated.jpg"
    ann_img.save(ann_path, quality=85)

    animal_detections = [d for d in detections if d["class"] in ANIMAL_CLASSES]
    label = "motion only"
    if animal_detections:
        counts = {}
        for d in animal_detections:
            counts[d["class"]] = counts.get(d["class"], 0) + 1
        label = ", ".join(f"{v} {k}" if v > 1 else k for k, v in sorted(counts.items()))
    elif detections:
        counts = {}
        for d in detections:
            counts[d["class"]] = counts.get(d["class"], 0) + 1
        label = ", ".join(f"{v} {k}" if v > 1 else k for k, v in sorted(counts.items()))

    meta = {
        "id": detection_id,
        "timestamp": datetime.now().isoformat(),
        "label": label,
        "detections": detections,
        "animal_detections": animal_detections,
    }
    (det_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    return meta


def _enrich_entry(det_dir: Path, meta: dict) -> None:
    meta["model"] = MODEL_NAME
    meta["conf_threshold"] = CONFIDENCE_THRESHOLD
    orig = det_dir / "original.jpg"
    if orig.exists():
        try:
            meta["bytes"] = orig.stat().st_size
        except OSError:
            pass
        try:
            with Image.open(orig) as im:
                meta["image_width"], meta["image_height"] = im.size
        except OSError:
            pass


@app.get("/api/detections")
async def list_detections(
    filter_class: str = Query(None, alias="class"),
    limit: int = Query(100, ge=1, le=1000),
):
    entries = []
    if not DETECTIONS_DIR.exists():
        return entries
    for det_dir in sorted(DETECTIONS_DIR.iterdir(), reverse=True):
        meta_path = det_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if filter_class:
            if not any(d["class"] == filter_class for d in meta.get("detections", [])):
                continue
        meta["thumbnail"] = f"/detections/{det_dir.name}/annotated.jpg"
        meta["original_url"] = f"/detections/{det_dir.name}/original.jpg"
        _enrich_entry(det_dir, meta)
        entries.append(meta)
        if len(entries) >= limit:
            break
    return entries


@app.delete("/api/detections/{detection_id}")
async def delete_detection(detection_id: str):
    if ".." in detection_id or "/" in detection_id or "\\" in detection_id:
        return JSONResponse({"error": "invalid id"}, status_code=400)
    det_path = (DETECTIONS_DIR / detection_id).resolve()
    if not str(det_path).startswith(str(DETECTIONS_DIR.resolve())):
        return JSONResponse({"error": "invalid id"}, status_code=400)
    if not det_path.is_dir():
        return JSONResponse({"error": "not found"}, status_code=404)
    shutil.rmtree(det_path)
    return {"ok": True}


@app.get("/detections/{detection_id}/{filename}")
async def serve_detection_image(detection_id: str, filename: str):
    filepath = DETECTIONS_DIR / detection_id / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(filepath, media_type="image/jpeg")


GALLERY_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backyard Detections</title>
<style>
:root{--bg:#111;--card:#1a1a2e;--accent:#e94560;--text:#eaeaea;--muted:#666}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh}
.hdr{padding:1rem 1.5rem;display:flex;align-items:center;justify-content:space-between;
     flex-wrap:wrap;gap:.75rem;border-bottom:1px solid #222}
.hdr h1{font-size:1.3rem;color:var(--accent)}
.hdr select,.hdr button{background:#222;color:var(--text);border:1px solid #333;border-radius:.4rem;
            padding:.4rem .6rem;font-size:.9rem;cursor:pointer}
.hdr button.danger{border-color:#a93226;color:#f5b7b1}
.controls{display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}
.meta-panel{font-size:.8rem;color:var(--muted);display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}
.meta-panel label{display:flex;align-items:center;gap:.35rem;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
      gap:1rem;padding:1.5rem}
.grid.highlight{display:block;padding:1.5rem}
.hero-wrap{margin-bottom:1.5rem}
.hero-wrap .hero-label{font-size:.85rem;color:var(--muted);margin-bottom:.5rem}
.hero{display:block;background:var(--card);border-radius:.75rem;overflow:hidden;max-width:min(960px,100%)}
.hero img{width:100%;max-height:70vh;object-fit:contain;cursor:pointer;display:block;background:#0a0a12}
.hero .info{padding:.75rem 1rem}
.hero .label{font-weight:700;font-size:1.1rem;margin-bottom:.25rem}
.hero .time{color:var(--muted);font-size:.85rem}
.subgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.75rem}
@media(max-width:900px){.subgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){.subgrid{grid-template-columns:1fr}}
.card{background:var(--card);border-radius:.75rem;overflow:hidden;transition:.2s}
.card:hover{transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.4)}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;cursor:pointer}
.card .info{padding:.75rem 1rem}
.card .label{font-weight:700;font-size:1rem;margin-bottom:.25rem}
.card .time{color:var(--muted);font-size:.8rem}
.card .label.animal{color:#58d68d}
.card .label.motion{color:#f0b030}
.card .actions{margin-top:.5rem;display:flex;gap:.5rem;align-items:center}
.card .meta-extra{font-size:.75rem;color:var(--muted);margin-top:.35rem;line-height:1.4}
.empty{text-align:center;color:var(--muted);padding:4rem;font-size:1.1rem}
.stats{color:var(--muted);font-size:.85rem}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:100;
         align-items:center;justify-content:center;cursor:pointer}
.overlay.show{display:flex}
.overlay img{max-width:95vw;max-height:95vh;object-fit:contain}
</style></head><body>
<div class="hdr">
  <h1>Backyard Detections</h1>
  <div class="controls">
    <span class="stats" id="stats"></span>
    <select id="layout" title="Layout">
      <option value="list">List</option>
      <option value="highlight_recent">Highlight recent</option>
    </select>
    <select id="filter" onchange="savePrefs();load()">
      <option value="">All</option>
      <option value="bird">Birds</option>
      <option value="cat">Cats</option>
      <option value="dog">Dogs</option>
      <option value="person">People</option>
    </select>
    <div class="meta-panel" id="metaPanel">
      <label><input type="checkbox" data-flag="relative"> Relative</label>
      <label><input type="checkbox" data-flag="iso"> Timestamp</label>
      <label><input type="checkbox" data-flag="conf"> Confidence</label>
      <label><input type="checkbox" data-flag="bbox"> Boxes</label>
      <label><input type="checkbox" data-flag="model"> Model</label>
      <label><input type="checkbox" data-flag="size"> Size</label>
    </div>
  </div>
</div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">No detections yet. Watching...</div>
<div class="overlay" id="overlay">
  <img id="overlay-img" alt="">
</div>
<script>
const LS='backyardGalleryPrefs';
const META_FLAGS=['relative','iso','conf','bbox','model','size'];
function parseMeta(s){
  if(!s)return new Set();
  return new Set(s.split(',').map(x=>x.trim()).filter(Boolean));
}
function readQS(){
  const p=new URLSearchParams(location.search);
  const m=p.get('meta');
  return {
    layout:p.get('layout')||'',
    filter:p.get('class')||'',
    meta:m?parseMeta(m):null,
  };
}
function loadPrefs(){
  let o={};
  try{o=JSON.parse(localStorage.getItem(LS)||'{}');}catch(e){}
  const q=readQS();
  document.getElementById('layout').value=q.layout||o.layout||'list';
  document.getElementById('filter').value=q.filter!==''?q.filter:(o.filter||'');
  const metaSet=q.meta||(o.metaFlags?new Set(o.metaFlags):new Set(['relative']));
  document.querySelectorAll('#metaPanel input[data-flag]').forEach(cb=>{
    cb.checked=metaSet.has(cb.dataset.flag);
  });
}
function getMetaSet(){
  const s=new Set();
  document.querySelectorAll('#metaPanel input[data-flag]').forEach(cb=>{
    if(cb.checked)s.add(cb.dataset.flag);
  });
  return s;
}
function savePrefs(){
  const layout=document.getElementById('layout').value;
  const filter=document.getElementById('filter').value;
  const metaSet=getMetaSet();
  const metaFlags=META_FLAGS.filter(f=>metaSet.has(f));
  localStorage.setItem(LS,JSON.stringify({layout,filter,metaFlags}));
  const p=new URLSearchParams();
  p.set('layout',layout);
  if(filter)p.set('class',filter);
  if(metaFlags.length)p.set('meta',metaFlags.join(','));
  const qs=p.toString();
  history.replaceState(null,'',qs?'?'+qs:location.pathname);
}
document.getElementById('layout').addEventListener('change',()=>{savePrefs();load();});
document.getElementById('filter').addEventListener('change',()=>{savePrefs();load();});
document.querySelectorAll('#metaPanel input[data-flag]').forEach(cb=>{
  cb.addEventListener('change',()=>{savePrefs();load();});
});
function fmt(iso){
  const d=new Date(iso);
  const now=new Date();
  const diff=now-d;
  if(diff<60000)return 'just now';
  if(diff<3600000)return Math.floor(diff/60000)+'m ago';
  if(diff<86400000)return Math.floor(diff/3600000)+'h ago';
  return d.toLocaleDateString()+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function fmtIso(iso){
  try{return new Date(iso).toLocaleString();}catch(e){return iso;}
}
function show(url){
  document.getElementById('overlay-img').src=url;
  document.getElementById('overlay').classList.add('show');
}
(function(){
  const ov=document.getElementById('overlay');
  const oimg=document.getElementById('overlay-img');
  ov.addEventListener('click',function(){ov.classList.remove('show');});
  oimg.addEventListener('click',function(e){e.stopPropagation();});
})();
document.getElementById('grid').addEventListener('click',function(ev){
  const img=ev.target.closest('img[data-full]');
  if(!img)return;
  ev.preventDefault();
  show(img.getAttribute('data-full'));
});
function fmtBytes(n){
  if(n==null)return '';
  if(n<1024)return n+' B';
  if(n<1048576)return (n/1024).toFixed(1)+' KB';
  return (n/1048576).toFixed(2)+' MB';
}
function extraMeta(d){
  const m=getMetaSet();
  const parts=[];
  if(m.has('iso'))parts.push(fmtIso(d.timestamp));
  if(m.has('model')&&d.model)parts.push('model '+d.model);
  if(m.has('size')){
    const bits=[];
    if(d.image_width)bits.push(d.image_width+'×'+d.image_height);
    if(d.bytes!=null)bits.push(fmtBytes(d.bytes));
    if(bits.length)parts.push(bits.join(' · '));
  }
  if(m.has('conf')){
    if(d.conf_threshold!=null)parts.push('conf ≥ '+d.conf_threshold);
    if(d.detections&&d.detections.length){
      parts.push('det '+d.detections.map(x=>x.class+':'+x.confidence).join(', '));
    }
  }
  if(m.has('bbox')&&d.detections&&d.detections.length){
    parts.push('bbox '+d.detections.map(x=>'['+x.bbox.join(',')+']').join(' '));
  }
  if(!parts.length)return '';
  return '<div class="meta-extra">'+parts.join(' · ')+'</div>';
}
function timeLine(d){
  return getMetaSet().has('relative')?('<div class="time">'+fmt(d.timestamp)+'</div>'):'';
}
function cardHtml(d){
  const isAnimal=d.animal_detections&&d.animal_detections.length>0;
  const id=JSON.stringify(d.id);
  return '<div class="card" data-id="'+escapeAttr(d.id)+'">'+
    '<img src="'+escapeAttr(d.thumbnail)+'" data-full="'+escapeAttr(d.original_url)+'" loading="lazy" alt="">'+
    '<div class="info">'+
    '<div class="label '+(isAnimal?'animal':'motion')+'">'+escapeHtml(d.label)+'</div>'+
    timeLine(d)+extraMeta(d)+
    '<div class="actions"><button type="button" class="danger" onclick="delDet('+id+',event)">Delete</button></div>'+
    '</div></div>';
}
function escapeHtml(s){
  const div=document.createElement('div');
  div.textContent=s;
  return div.innerHTML;
}
function escapeAttr(s){
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
}
async function delDet(id,ev){
  ev.stopPropagation();
  if(!confirm('Delete this detection permanently?'))return;
  const res=await fetch('/api/detections/'+encodeURIComponent(id),{method:'DELETE'});
  if(!res.ok){alert('Delete failed');return;}
  load();
}
async function load(){
  savePrefs();
  const cls=document.getElementById('filter').value;
  const layout=document.getElementById('layout').value;
  const url='/api/detections'+(cls?'?class='+encodeURIComponent(cls):'');
  const res=await fetch(url);
  const data=await res.json();
  const grid=document.getElementById('grid');
  const empty=document.getElementById('empty');
  const stats=document.getElementById('stats');
  grid.className='grid'+(layout==='highlight_recent'?' highlight':'');
  if(!data.length){grid.innerHTML='';empty.style.display='block';stats.textContent='';return;}
  empty.style.display='none';
  stats.textContent=data.length+' detection'+(data.length!==1?'s':'');
  if(layout==='highlight_recent'&&data.length){
    const hero=data[0];
    const rest=data.slice(1);
    const isAnimal=hero.animal_detections&&hero.animal_detections.length>0;
    const heroId=JSON.stringify(hero.id);
    const heroBlock=
      '<div class="hero-wrap"><div class="hero-label">Most recent</div>'+
      '<div class="card hero">'+
      '<img src="'+escapeAttr(hero.thumbnail)+'" data-full="'+escapeAttr(hero.original_url)+'" loading="eager" alt="">'+
      '<div class="info">'+
      '<div class="label '+(isAnimal?'animal':'motion')+'">'+escapeHtml(hero.label)+'</div>'+
      timeLine(hero)+extraMeta(hero)+
      '<div class="actions"><button type="button" class="danger" onclick="delDet('+heroId+',event)">Delete</button></div>'+
      '</div></div></div>';
    const sub=rest.map(d=>cardHtml(d)).join('');
    grid.innerHTML=heroBlock+(rest.length?'<div class="subgrid">'+sub+'</div>':'');
  }else{
    grid.innerHTML=data.map(d=>cardHtml(d)).join('');
  }
}
loadPrefs();
load();
setInterval(load,10000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def gallery():
    return GALLERY_HTML


if __name__ == "__main__":
    get_model()
    uvicorn.run(app, host="0.0.0.0", port=8089)