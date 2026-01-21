from fastapi import FastAPI

app = FastAPI(title="QuartzCouncil")

@app.get("/health")
def health():
    return {"ok": True, "service": "QuartzCouncil"}