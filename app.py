from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def health():
    return {"health": "ok"}

class EchoIn(BaseModel):
    text: str

@app.post("/echo")
def echo(body: EchoIn):
    return {"echo": body.text}
