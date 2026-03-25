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
        entries.append(meta)
        if len(entries) >= limit:
            break
    return entries


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
     border-bottom:1px solid #222}
.hdr h1{font-size:1.3rem;color:var(--accent)}
.hdr select{background:#222;color:var(--text);border:1px solid #333;border-radius:.4rem;
            padding:.4rem .6rem;font-size:.9rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
      gap:1rem;padding:1.5rem}
.card{background:var(--card);border-radius:.75rem;overflow:hidden;transition:.2s}
.card:hover{transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.4)}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;cursor:pointer}
.card .info{padding:.75rem 1rem}
.card .label{font-weight:700;font-size:1rem;margin-bottom:.25rem}
.card .time{color:var(--muted);font-size:.8rem}
.card .label.animal{color:#58d68d}
.card .label.motion{color:#f0b030}
.empty{text-align:center;color:var(--muted);padding:4rem;font-size:1.1rem}
.stats{color:var(--muted);font-size:.85rem}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:100;
         align-items:center;justify-content:center;cursor:pointer}
.overlay.show{display:flex}
.overlay img{max-width:95vw;max-height:95vh;object-fit:contain}
</style></head><body>
<div class="hdr">
  <h1>Backyard Detections</h1>
  <div style="display:flex;gap:.75rem;align-items:center">
    <span class="stats" id="stats"></span>
    <select id="filter" onchange="load()">
      <option value="">All</option>
      <option value="bird">Birds</option>
      <option value="cat">Cats</option>
      <option value="dog">Dogs</option>
      <option value="person">People</option>
    </select>
  </div>
</div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">No detections yet. Watching...</div>
<div class="overlay" id="overlay" onclick="this.classList.remove('show')">
  <img id="overlay-img">
</div>
<script>
function fmt(iso){
  const d=new Date(iso);
  const now=new Date();
  const diff=now-d;
  if(diff<60000)return 'just now';
  if(diff<3600000)return Math.floor(diff/60000)+'m ago';
  if(diff<86400000)return Math.floor(diff/3600000)+'h ago';
  return d.toLocaleDateString()+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function show(url){
  document.getElementById('overlay-img').src=url;
  document.getElementById('overlay').classList.add('show');
}
async function load(){
  const cls=document.getElementById('filter').value;
  const url='/api/detections'+(cls?'?class='+cls:'');
  const res=await fetch(url);
  const data=await res.json();
  const grid=document.getElementById('grid');
  const empty=document.getElementById('empty');
  const stats=document.getElementById('stats');
  if(!data.length){grid.innerHTML='';empty.style.display='block';stats.textContent='';return;}
  empty.style.display='none';
  stats.textContent=data.length+' detection'+(data.length!==1?'s':'');
  grid.innerHTML=data.map(d=>{
    const isAnimal=d.animal_detections&&d.animal_detections.length>0;
    return `<div class="card">
      <img src="${d.thumbnail}" onclick="show('${d.original_url}')" loading="lazy">
      <div class="info">
        <div class="label ${isAnimal?'animal':'motion'}">${d.label}</div>
        <div class="time">${fmt(d.timestamp)}</div>
      </div></div>`;
  }).join('');
}
load();
setInterval(load,10000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def gallery():
    return GALLERY_HTML


if __name__ == "__main__":
    get_model()
    uvicorn.run(app, host="0.0.0.0", port=8089)
