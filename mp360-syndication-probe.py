#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from curl_cffi import requests as curl_requests
import requests

OUT = Path("discovery/syndication-probe")
OUT.mkdir(parents=True, exist_ok=True)

SOURCES = {
    "forrent": "https://www.forrent.com/or/portland/cyan-pdx/5jsnltp",
    "corporatehousing": "https://www.corporatehousing.com/or/portland/cyan-pdx/5jsnltp",
    "homes": "https://www.homes.com/property/cyan-pdx-portland-or/8f8z1fck4mtk9/",
    "greystar": "https://www.greystar.com/cyan-pdx-apartments-portland-or/p_12248",
    "cyan-floorplans": "https://www.cyanpdx.com/floorplans",
    "cyan-home": "https://www.cyanpdx.com/",
    "apartmenthomeliving": "https://www.apartmenthomeliving.com/apartment-finder/Cyan-PDX-Portland-OR-97201-1917738",
}

KEYWORD = re.compile(
    r"matterport|virtual.?tour|3d.?tour|services/3d-tours|modelSid|matterportModelId|spaceSid|tourId|AS2|AT2|\bA5\b|\bB1\b|\bB3\b|\bC1\b",
    re.I,
)
SID_PATTERNS = [
    re.compile(r"(?:my\.)?matterport\.com/(?:show|discover)/?\?[^\s\"'<>]{0,2200}?[?&]m=([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"my\.matterport\.com/api/v1/player/models/([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"my\.matterport\.com/(?:models|spaces)/([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"/services/3d-tours/[A-Za-z0-9_-]+/([A-Za-z0-9_-]{11})(?:[/?#\"' ]|$)", re.I),
    re.compile(r"[\"'](?:modelSid|modelId|matterportModelId|matterportId|matterport_id|spaceSid|spaceId|tourId|tourCode)[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]{11})[\"']", re.I),
]


def variants(text: str) -> list[str]:
    output = [text, html.unescape(text).replace("\\u0026", "&").replace("\\/", "/")]
    current = output[-1]
    for _ in range(5):
        try:
            decoded = unquote(current)
        except Exception:
            break
        if decoded == current:
            break
        output.append(decoded)
        current = decoded
    return output


def analyze(name: str, method: str, body: str, response_url: str, status: int, content_type: str) -> dict:
    stem = f"{name}-{method}"
    (OUT / f"{stem}.html").write_text(body, errors="replace")
    sids: dict[str, list[str]] = {}
    snippets: list[str] = []
    links: list[str] = []
    scripts: list[str] = []
    iframes: list[str] = []
    for variant_index, text in enumerate(variants(body)):
        for pattern in SID_PATTERNS:
            for match in pattern.finditer(text):
                sid = match.group(1)
                context = re.sub(
                    r"\s+",
                    " ",
                    text[max(0, match.start() - 600) : min(len(text), match.end() + 1200)],
                )
                sids.setdefault(sid, []).append(f"variant-{variant_index}: {context[:2400]}")
        for match in KEYWORD.finditer(text):
            context = re.sub(
                r"\s+",
                " ",
                text[max(0, match.start() - 750) : min(len(text), match.end() + 1600)],
            )
            if context not in snippets:
                snippets.append(context[:3200])
            if len(snippets) >= 400:
                break
        for match in re.finditer(r"<(?:a|link)[^>]+href=[\"']([^\"']+)[\"']", text, re.I):
            link = urljoin(response_url, html.unescape(match.group(1)))
            if link not in links:
                links.append(link)
        for match in re.finditer(r"<script[^>]+src=[\"']([^\"']+)[\"']", text, re.I):
            script = urljoin(response_url, html.unescape(match.group(1)))
            if script not in scripts:
                scripts.append(script)
        for match in re.finditer(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", text, re.I):
            iframe = urljoin(response_url, html.unescape(match.group(1)))
            if iframe not in iframes:
                iframes.append(iframe)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    report = {
        "source": name,
        "method": method,
        "requestedUrl": SOURCES[name],
        "status": status,
        "responseUrl": response_url,
        "contentType": content_type,
        "bytes": len(body.encode("utf-8", errors="replace")),
        "title": re.sub(r"\s+", " ", title_match.group(1))[:500] if title_match else None,
        "matterportCount": len(re.findall(r"matterport", body, re.I)),
        "virtualTourCount": len(re.findall(r"virtual.?tour", body, re.I)),
        "service3dCount": len(re.findall(r"services/3d-tours", body, re.I)),
        "sids": sids,
        "iframes": iframes,
        "relevantLinks": [link for link in links if KEYWORD.search(link)][:500],
        "scriptUrls": scripts[:500],
        "snippets": snippets,
    }
    (OUT / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def fetch_curl(url: str, impersonate: str) -> tuple[str, str, int, str]:
    response = curl_requests.get(
        url,
        impersonate=impersonate,
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=120,
        allow_redirects=True,
    )
    return response.text, response.url, response.status_code, response.headers.get("content-type", "")


def fetch_plain(url: str) -> tuple[str, str, int, str]:
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=120,
        allow_redirects=True,
    )
    return response.text, response.url, response.status_code, response.headers.get("content-type", "")


def probe_scripts(reports: list[dict]) -> dict:
    candidates = []
    seen = set()
    for report in sorted(
        reports,
        key=lambda item: (
            len(item.get("sids", {})),
            item.get("matterportCount", 0),
            item.get("virtualTourCount", 0),
            item.get("bytes", 0),
        ),
        reverse=True,
    ):
        for script in report.get("scriptUrls", []):
            if script not in seen:
                candidates.append(script)
                seen.add(script)
    results = []
    discovered_sids: dict[str, list[str]] = {}
    endpoints = set()
    for index, script_url in enumerate(candidates[:200]):
        try:
            response = curl_requests.get(script_url, impersonate="chrome", timeout=90)
            body = response.text
        except Exception as error:
            results.append({"url": script_url, "error": str(error)})
            continue
        local_sids = {}
        for text in variants(body):
            for pattern in SID_PATTERNS:
                for match in pattern.finditer(text):
                    sid = match.group(1)
                    local_sids.setdefault(sid, []).append(script_url)
                    discovered_sids.setdefault(sid, []).append(script_url)
            for match in re.finditer(
                r"(?:https?://[^\s\"']+|/[A-Za-z0-9_./?=&%:-]{4,})",
                text,
                re.I,
            ):
                value = match.group(0)[:2000]
                if re.search(r"matterport|3d-tour|virtual-tour|virtualtour|tourmedia|media.*tour|tour.*media", value, re.I):
                    endpoints.add(value)
        if KEYWORD.search(body) or local_sids:
            (OUT / f"script-{index}.js").write_text(body, errors="replace")
            results.append({
                "url": script_url,
                "status": response.status_code,
                "bytes": len(response.content),
                "sids": local_sids,
            })
    result = {
        "scriptsProbed": len(candidates[:200]),
        "scriptsWithRelevantContent": results,
        "discoveredSids": discovered_sids,
        "candidateEndpoints": sorted(endpoints),
    }
    (OUT / "scripts-summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def validate_sids(sids: set[str]) -> dict:
    validation = {}
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    for sid in sorted(sids):
        try:
            response = session.get(
                f"https://my.matterport.com/api/v1/player/models/{sid}/thumb",
                timeout=30,
                allow_redirects=True,
            )
            validation[sid] = {
                "status": response.status_code,
                "url": response.url,
                "contentType": response.headers.get("content-type"),
                "bytes": len(response.content),
                "valid": response.status_code == 200 and (
                    "image" in (response.headers.get("content-type") or "").lower()
                    or len(response.content) > 2000
                ),
            }
        except Exception as error:
            validation[sid] = {"valid": False, "error": str(error)}
    return validation


def main() -> None:
    reports = []
    for name, url in SOURCES.items():
        methods = [
            ("curl-chrome", lambda url=url: fetch_curl(url, "chrome")),
            ("plain", lambda url=url: fetch_plain(url)),
        ]
        for method_name, function in methods:
            try:
                body, response_url, status, content_type = function()
                report = analyze(name, method_name, body, response_url, status, content_type)
                reports.append(report)
                print(json.dumps({
                    "source": name,
                    "method": method_name,
                    "status": status,
                    "bytes": report["bytes"],
                    "title": report["title"],
                    "matterportCount": report["matterportCount"],
                    "virtualTourCount": report["virtualTourCount"],
                    "sids": sorted(report["sids"]),
                    "iframeCount": len(report["iframes"]),
                }), flush=True)
            except Exception as error:
                reports.append({
                    "source": name,
                    "method": method_name,
                    "requestedUrl": url,
                    "error": str(error),
                })
                print(f"FETCH_ERROR source={name} method={method_name} error={error}", flush=True)

    scripts = probe_scripts(reports)
    all_sids = {
        sid
        for report in reports
        for sid in report.get("sids", {})
    } | set(scripts.get("discoveredSids", {}))
    validation = validate_sids(all_sids)
    valid_sids = [sid for sid, item in validation.items() if item.get("valid")]
    summary = {
        "reports": [
            {
                key: report.get(key)
                for key in (
                    "source",
                    "method",
                    "requestedUrl",
                    "status",
                    "responseUrl",
                    "contentType",
                    "bytes",
                    "title",
                    "matterportCount",
                    "virtualTourCount",
                    "service3dCount",
                    "sids",
                    "iframes",
                    "relevantLinks",
                    "error",
                )
                if key in report
            }
            for report in reports
        ],
        "scriptProbe": scripts,
        "validation": validation,
        "validSids": valid_sids,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (OUT / "models.txt").write_text("\n".join(valid_sids) + ("\n" if valid_sids else ""))
    print("SYNDICATION_PROBE_RESULT=" + json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
