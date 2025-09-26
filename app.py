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

from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import os, json
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

app = FastAPI()

# ==== 既存 ====
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

# ==== PubMed E-utilities ====
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_TOOL = "render-pubmed-api"
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "shuyat.3.14@gmail.com")   # 推奨: Koyeb/Renderの環境変数に実メールを設定
NCBI_API_KEY = os.getenv("NCBI_API_KEY")                      # 任意: 所持していれば設定（レート上限が緩くなる）

def _common_params():
    p = {"tool": NCBI_TOOL, "email": NCBI_EMAIL}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8))
async def _get_json(client: httpx.AsyncClient, url: str, params: dict):
    r = await client.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8))
async def _get_text(client: httpx.AsyncClient, url: str, params: dict):
    r = await client.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.text

# --- 1) 検索: esearch ---
@app.get("/pubmed/search")
async def pubmed_search(
    query: str = Query(..., description="PubMed検索式"),
    retmax: int = 20,
    retstart: int = 0,
    sort: str = "relevance",   # relevance | pub+date | etc.
    datetype: str = "edat",    # edat, pdat など
    mindate: Optional[str] = None,  # 例: 2020/01/01
    maxdate: Optional[str] = None
):
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": retmax,
        "retstart": retstart,
        "sort": sort,
        "datetype": datetype,
        **_common_params(),
    }
    if mindate: params["mindate"] = mindate
    if maxdate: params["maxdate"] = maxdate

    async with httpx.AsyncClient() as client:
        data = await _get_json(client, f"{NCBI_BASE}/esearch.fcgi", params)

    ids = data.get("esearchresult", {}).get("idlist", [])
    count = int(data.get("esearchresult", {}).get("count", 0))
    return {"count": count, "pmids": ids, "query": query}

# --- 2) 概要: esummary ---
@app.get("/pubmed/summary")
async def pubmed_summary(ids: str = Query(..., description="カンマ区切りPMID列（例: 12345,23456）")):
    params = {"db": "pubmed", "id": ids, "retmode": "json", **_common_params()}
    async with httpx.AsyncClient() as client:
        data = await _get_json(client, f"{NCBI_BASE}/esummary.fcgi", params)

    result = []
    uids = data.get("result", {}).get("uids", [])
    for uid in uids:
        item = data["result"].get(uid, {})
        result.append({
            "pmid": uid,
            "title": item.get("title"),
            "journal": item.get("fulljournalname") or item.get("source"),
            "pubdate": item.get("pubdate"),
            "authors": [a.get("name") for a in item.get("authors", []) if a.get("name")],
            "doi": (item.get("elocationid") or ""),
        })
    return {"records": result}

# --- 3) 抄録: efetch（JSON整形） ---
@app.get("/pubmed/abstract")
async def pubmed_abstract(ids: str = Query(...)):
    params = {
        "db": "pubmed",
        "id": ids,
        "retmode": "xml",   # XMLで取得して素朴に抽出
        "rettype": "abstract",
        **_common_params(),
    }
    async with httpx.AsyncClient() as client:
        xml = await _get_text(client, f"{NCBI_BASE}/efetch.fcgi", params)

    # 依存を増やさずに超簡易抽出（実運用では正規XMLパーサ推奨）
    # <ArticleTitle> と <AbstractText> をザックリ抜く
    import re
    articles = []
    for art in re.findall(r"<PubmedArticle>(.*?)</PubmedArticle>", xml, flags=re.S):
        title = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", art, flags=re.S)
        title = re.sub(r"<.*?>", "", title.group(1)).strip() if title else ""
        abstr = "".join(re.findall(r"<AbstractText.*?>(.*?)</AbstractText>", art, flags=re.S))
        abstr = re.sub(r"<.*?>", "", abstr).strip()
        pmid = re.search(r"<PMID.*?>(\d+)</PMID>", art)
        pmid = pmid.group(1) if pmid else ""
        articles.append({"pmid": pmid, "title": title, "abstract": abstr})
    if not articles:
        raise HTTPException(status_code=404, detail="No abstracts parsed")
    return {"records": articles}

# --- 4) 便利合体: 検索→上位N件のタイトル/抄録まとめ ---
@app.get("/pubmed/search_and_fetch")
async def pubmed_search_and_fetch(
    query: str,
    retmax: int = 10,
    sort: str = "relevance",
    mindate: Optional[str] = None,
    maxdate: Optional[str] = None,
    datetype: str = "edat"
):
    srch = await pubmed_search(query=query, retmax=retmax, sort=sort, mindate=mindate, maxdate=maxdate, datetype=datetype)
    if not srch["pmids"]:
        return {"count": srch["count"], "records": []}

    ids = ",".join(srch["pmids"])
    summ = await pubmed_summary(ids=ids)
    abstr = await pubmed_abstract(ids=ids)

    # pmidで突合
    abs_map = {a["pmid"]: a for a in abstr["records"]}
    merged = []
    for rec in summ["records"]:
        pmid = rec["pmid"]
        merged.append({
            **rec,
            "abstract": abs_map.get(pmid, {}).get("abstract", "")
        })
    return {"count": srch["count"], "records": merged, "query": query}
