#!/usr/bin/env python3
from __future__ import annotations

import html as html_module
import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin

from curl_cffi import requests as curl_requests
import requests

OUT = Path("discovery/http-probe")
OUT.mkdir(parents=True, exist_ok=True)
URL = "https://www.apartments.com/cyan-pdx-portland-or/hjsnmt2/"

KEYWORDS = re.compile(
    r"matterport|virtual.?tour|3d.?tour|services/3d-tours|modelSid|matterportModelId|spaceSid|tourId|AS2|AT2|\bA5\b|\bB1\b|\bB3\b|\bC1\b",
    re.I,
)
SID_PATTERNS = [
    re.compile(r"(?:my\.)?matterport\.com/(?:show|discover)/?\?[^\s\"'<>]{0,2000}?[?&]m=([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"my\.matterport\.com/api/v1/player/models/([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"/services/3d-tours/[A-Za-z0-9_-]+/([A-Za-z0-9_-]{11})(?:[/?#\"' ]|$)", re.I),
    re.compile(r"[\"'](?:modelSid|modelId|matterportModelId|matterportId|spaceSid|spaceId|tourId)[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]{11})[\"']", re.I),
]


def decode_variants(text: str) -> list[str]:
    variants = [text, html_module.unescape(text).replace("\\u0026", "&").replace("\\/", "/")]
    current = variants[-1]
    for _ in range(5):
        try:
            decoded = unquote(current)
        except Exception:
            break
        if decoded == current:
            break
        variants.append(decoded)
        current = decoded
    return variants


def inspect_body(name: str, body: str, final_url: str, status: int, content_type: str) -> dict:
    body_path = OUT / f"{name}.html"
    body_path.write_text(body, errors="replace")
    sids: dict[str, list[str]] = {}
    snippets: list[str] = []
    script_urls: list[str] = []
    for variant_index, variant in enumerate(decode_variants(body)):
        for pattern in SID_PATTERNS:
            for match in pattern.finditer(variant):
                sid = match.group(1)
                context = re.sub(
                    r"\s+",
                    " ",
                    variant[max(0, match.start() - 500) : min(len(variant), match.end() + 900)],
                )
                sids.setdefault(sid, []).append(f"v{variant_index}: {context[:1800]}")
        for match in KEYWORDS.finditer(variant):
            context = re.sub(
                r"\s+",
                " ",
                variant[max(0, match.start() - 600) : min(len(variant), match.end() + 1200)],
            )
            if context not in snippets:
                snippets.append(context[:2400])
            if len(snippets) >= 300:
                break
        for match in re.finditer(r"<script[^>]+src=[\"']([^\"']+)[\"']", variant, re.I):
            script_url = urljoin(final_url, html_module.unescape(match.group(1)))
            if script_url not in script_urls:
                script_urls.append(script_url)
    report = {
        "name": name,
        "status": status,
        "finalUrl": final_url,
        "contentType": content_type,
        "bytes": len(body.encode("utf-8", errors="replace")),
        "title": (re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S).group(1)[:500] if re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S) else None),
        "matterportCount": len(re.findall(r"matterport", body, re.I)),
        "virtualTourCount": len(re.findall(r"virtual.?tour", body, re.I)),
        "service3dCount": len(re.findall(r"services/3d-tours", body, re.I)),
        "sids": sids,
        "snippets": snippets,
        "scriptUrls": script_urls,
    }
    (OUT / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def fetch_requests() -> tuple[str, str, int, str]:
    response = requests.get(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=90,
        allow_redirects=True,
    )
    return response.text, response.url, response.status_code, response.headers.get("content-type", "")


def fetch_curl(impersonate: str) -> tuple[str, str, int, str]:
    response = curl_requests.get(
        URL,
        impersonate=impersonate,
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=90,
        allow_redirects=True,
    )
    return response.text, response.url, response.status_code, response.headers.get("content-type", "")


def fetch_jina(target: str) -> tuple[str, str, int, str]:
    response = requests.get(target, timeout=120, allow_redirects=True)
    return response.text, response.url, response.status_code, response.headers.get("content-type", "")


def inspect_scripts(script_urls: list[str]) -> dict:
    results = []
    candidate_endpoints = set()
    for index, script_url in enumerate(script_urls[:120]):
        try:
            response = curl_requests.get(script_url, impersonate="chrome", timeout=60)
            body = response.text
        except Exception as error:
            results.append({"url": script_url, "error": str(error)})
            continue
        matches = []
        for pattern in [
            r"https?://[^\s\"']+",
            r"/[A-Za-z0-9_./?=&%-]*(?:3d-tour|virtual-tour|matterport|media|listing|property|model)[A-Za-z0-9_./?=&%-]*",
        ]:
            for match in re.finditer(pattern, body, re.I):
                value = match.group(0)[:1200]
                if value not in matches:
                    matches.append(value)
                if re.search(r"3d-tour|virtual-tour|matterport", value, re.I):
                    candidate_endpoints.add(value)
                if len(matches) >= 100:
                    break
        if KEYWORDS.search(body):
            (OUT / f"script-{index}.js").write_text(body, errors="replace")
            results.append({
                "url": script_url,
                "status": response.status_code,
                "bytes": len(response.content),
                "matches": matches,
            })
    report = {"scriptsWithKeywords": results, "candidateEndpoints": sorted(candidate_endpoints)}
    (OUT / "scripts.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    reports = []
    methods = [
        ("requests", fetch_requests),
        ("curl-chrome", lambda: fetch_curl("chrome")),
        ("curl-chrome124", lambda: fetch_curl("chrome124")),
        ("curl-safari", lambda: fetch_curl("safari17_0")),
        ("jina-https", lambda: fetch_jina("https://r.jina.ai/http://https://www.apartments.com/cyan-pdx-portland-or/hjsnmt2/")),
        ("jina-http", lambda: fetch_jina("https://r.jina.ai/http://www.apartments.com/cyan-pdx-portland-or/hjsnmt2/")),
    ]
    for name, function in methods:
        try:
            body, final_url, status, content_type = function()
            report = inspect_body(name, body, final_url, status, content_type)
            reports.append(report)
            print(json.dumps({
                "name": name,
                "status": status,
                "bytes": report["bytes"],
                "title": report["title"],
                "matterportCount": report["matterportCount"],
                "virtualTourCount": report["virtualTourCount"],
                "service3dCount": report["service3dCount"],
                "sids": sorted(report["sids"]),
            }), flush=True)
        except Exception as error:
            reports.append({"name": name, "error": str(error)})
            print(f"FETCH_ERROR {name}: {error}", flush=True)

    best = max(
        (report for report in reports if "error" not in report),
        key=lambda report: (
            len(report.get("sids", {})),
            report.get("matterportCount", 0),
            report.get("virtualTourCount", 0),
            report.get("bytes", 0),
        ),
        default=None,
    )
    scripts = inspect_scripts(best.get("scriptUrls", []) if best else [])
    summary = {
        "reports": [
            {
                key: report.get(key)
                for key in (
                    "name",
                    "status",
                    "finalUrl",
                    "contentType",
                    "bytes",
                    "title",
                    "matterportCount",
                    "virtualTourCount",
                    "service3dCount",
                    "sids",
                    "scriptUrls",
                    "error",
                )
                if key in report
            }
            for report in reports
        ],
        "best": best.get("name") if best else None,
        "scriptProbe": scripts,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("HTTP_PROBE_RESULT=" + json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
