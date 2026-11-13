"""Candidate-source retrieval and AI evidence assessment; never auto-verifies facts."""
from __future__ import annotations
import ipaddress, json, re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
import requests
from pydantic import BaseModel, ConfigDict, Field
from .director_models import SourceEvidenceBatch
from .llm_structured import StructuredLLMRouter

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style", "noscript", "svg"): self.skip += 1
    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript", "svg") and self.skip: self.skip -= 1
    def handle_data(self, data):
        if not self.skip and data.strip(): self.parts.append(data.strip())

def _safe_public_url(url: str) -> tuple[bool, str]:
    parsed=urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "invalid_url"
    host=parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False, "local_host_blocked"
    try:
        ip=ipaddress.ip_address(host)
        if not ip.is_global: return False, "non_public_ip_blocked"
    except ValueError:
        # Hostname DNS resolution is left to the operating system. Redirects are
        # deliberately not followed, preventing open-redirect access to local hosts.
        pass
    return True, "ok"

def fetch_source_excerpt(url: str, max_bytes: int=700_000, max_chars: int=5000) -> dict[str, Any]:
    safe, reason=_safe_public_url(url)
    if not safe: return {"url":url,"status":reason,"text":""}
    try:
        with requests.get(url, timeout=(5, 12), stream=True, allow_redirects=False,
                          headers={"User-Agent":"AI-Video-Factory-RU/1.0"}) as response:
            if 300 <= response.status_code < 400:
                return {"url":url,"status":f"redirect_{response.status_code}_not_followed","text":""}
            content_type=response.headers.get("Content-Type","").lower()
            if response.status_code >= 400:
                return {"url":url,"status":f"http_{response.status_code}","text":""}
            if "pdf" in content_type or "application/octet-stream" in content_type:
                return {"url":url,"status":"unsupported_document_type","text":""}
            chunks=[]; size=0
            for chunk in response.iter_content(32_768):
                if not chunk: continue
                size += len(chunk)
                if size > max_bytes: break
                chunks.append(chunk)
            raw=b"".join(chunks)
            encoding=response.encoding or "utf-8"
            html=raw.decode(encoding,errors="replace")
            parser=_TextExtractor(); parser.feed(html)
            text=re.sub(r"\s+"," "," ".join(parser.parts)).strip()[:max_chars]
            if not text: return {"url":url,"status":"empty_text","text":""}
            return {"url":url,"status":f"http_{response.status_code}","text":text}
    except requests.RequestException as exc:
        return {"url":url,"status":"fetch_error","text":"","error":str(exc)[:300]}

def audit_claim_sources(settings: Any, plans: list[dict[str, Any]], output_path) -> dict[str, Any]:
    candidates=[]; results=[]
    for plan in plans:
        for ci, claim in enumerate(plan.get("claims", []),1):
            urls=(claim.get("source_urls") or [])[:3]
            if not urls:
                results.append({"scene_id":plan["scene_id"],"claim":claim.get("text", ""),
                    "source_url":None,"url_status":"no_source_candidate","ai_verdict":"insufficient",
                    "evidence_quote":"","reasoning":"Источник-кандидат не указан.",
                    "verified_by_human":False,"status":"human_review_required"})
            for ui,url in enumerate(urls,1):
                page=fetch_source_excerpt(url)
                candidates.append({"source_id":f"{plan['scene_id']}_{ci}_{ui}",
                    "scene_id":plan["scene_id"],"claim":claim.get("text", ""),
                    "url":url,"url_status":page["status"],"page_text":page["text"]})
    system="""Ты — аналитик источников. Сопоставь точный claim с предоставленным текстом страницы.
Верни supports только если фрагмент страницы прямо подтверждает утверждение, contradicts
если прямо противоречит, иначе insufficient; если текст не получен — unavailable.
evidence_quote должен быть дословной короткой цитатой из page_text, иначе оставь пустым.
Не используй внешние знания и не делай вывод о достоверности всего источника.
Это предварительная AI-оценка, а не фактчекинг и не подтверждение человеком."""
    # Small groups keep source excerpts and completions bounded for MWS/Ollama.
    for offset in range(0,len(candidates),3):
        batch=candidates[offset:offset+3]
        payload=[{k:x[k] for k in ("source_id","scene_id","claim","url","url_status","page_text")} for x in batch]
        assessment=StructuredLLMRouter(settings).complete(
            SourceEvidenceBatch,system,"Оцени источники:\n"+json.dumps(payload,ensure_ascii=False),
            temperature=0.0,max_tokens=2600).assessments
        expected={x["source_id"] for x in batch}
        if {x.source_id for x in assessment} != expected or len(assessment)!=len(expected):
            raise ValueError("Source auditor вернул неполный набор source_id")
        by_id={x.source_id:x.model_dump(mode="json") for x in assessment}
        for src in batch:
            ev=by_id[src["source_id"]]
            quote=ev.get("evidence_quote","")
            quote_match=bool(quote and quote.casefold() in src["page_text"].casefold())
            verdict=ev["verdict"] if quote_match or not quote else "insufficient"
            results.append({"scene_id":src["scene_id"],"claim":src["claim"],
                "source_url":src["url"],"url_status":src["url_status"],
                "ai_verdict":verdict,"evidence_quote":quote if quote_match else "",
                "quote_match":quote_match,"reasoning":ev["reasoning"],
                "verified_by_human":False,"status":"human_review_required"})
    report={"status":"completed_not_fact_verified","entries":results,
            "facts_verified":False,"human_review_required":True,
            "policy":"URL reachability and AI assessment are evidence leads, never factual approval."}
    output_path.parent.mkdir(parents=True,exist_ok=True)
    output_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report
