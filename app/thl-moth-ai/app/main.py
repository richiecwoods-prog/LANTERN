from pathlib import Path
from fastapi import FastAPI, File, Request, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import APP_NAME, DEFAULT_CENTER, UPLOAD_DIR
from .processor import analyse, load_moth_file

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATE = {"latest": None, "filename": None}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "center": DEFAULT_CENTER})

@app.get("/health")
def health():
    return {"status": "ONLINE", "app": APP_NAME, "ai_hat_mode": "ready_for_hailo_runtime_optional"}

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".csv", ".json", ".geojson")):
        raise HTTPException(status_code=400, detail="Upload CSV, JSON or GeoJSON only.")
    dest = UPLOAD_DIR / Path(file.filename).name
    dest.write_bytes(await file.read())
    try:
        df = load_moth_file(dest)
        result = analyse(df)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    STATE["latest"] = result
    STATE["filename"] = file.filename
    return JSONResponse({"filename": file.filename, **result})

@app.get("/latest")
def latest():
    return STATE if STATE["latest"] else {"latest": None, "filename": None}
