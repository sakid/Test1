#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from playwright.async_api import BrowserContext, Locator, Page, async_playwright

OUTPUT = Path("work/discovery")
OUTPUT.mkdir(parents=True, exist_ok=True)

SOURCES = [
    "https://www.apartments.com/cyan-pdx-portland-or/hjsnmt2/",
    "https://www.cyanpdx.com/floorplans",
    "https://www.greystar.com/cyan-pdx-apartments-portland-or/p_12248",
    "https://www.forrent.com/or/portland/cyan-pdx/5jsnltp",
    "https://www.corporatehousing.com/or/portland/cyan-pdx/5jsnltp",
    "https://www.homes.com/property/cyan-pdx-portland-or/8f8z1fck4mtk9/",
]

PLANS = [
    "Studio (AS2)",
    "Studio AS2",
    "One Bedroom (A5)",
    "One Bedroom A5",
    "Large One Bedroom Townhome (AT2)",
    "Townhome AT2",
    "Two Bedroom, One Bath (B1)",
    "Two Bedroom B1",
    "Two Bedroom, Two Bath (B3)",
    "Two Bedroom B3",
    "Three Bedroom (C1)",
    "Three Bedroom C1",
]

KNOWN_FITNESS = "i1MqSM99sWw"
VALID_SID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MATTERPORT_URL_PATTERNS = [
    re.compile(
        r"(?:https?:)?//(?:my\.)?matterport\.com/(?:show|discover)/?\?[^\s\"'<>]{0,1800}?[?&]m=([A-Za-z0-9_-]{11})",
        re.I,
    ),
    re.compile(r"my\.matterport\.com/api/v1/player/models/([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"my\.matterport\.com/(?:models|spaces)/([A-Za-z0-9_-]{11})", re.I),
    re.compile(
        r"[\"'](?:modelSid|modelId|matterportModelId|matterportId|matterport_id|spaceSid|spaceId)[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]{11})[\"']",
        re.I,
    ),
]

records: dict[str, dict[str, Any]] = {}
network_log: list[dict[str, str]] = []
pending_tasks: set[asyncio.Task[Any]] = set()
state = {"action": "startup"}


def add_sid(sid: object, source: str, context: str = "", action: str | None = None) -> None:
    sid = str(sid or "").strip()
    if not VALID_SID.fullmatch(sid):
        return
    action = action or state["action"]
    item = records.setdefault(
        sid,
        {
            "modelSid": sid,
            "sources": [],
            "actions": [],
            "contexts": [],
            "validPublicModel": None,
        },
    )
    source = str(source)[:500]
    context = re.sub(r"\s+", " ", str(context))[:1200]
    if source and source not in item["sources"]:
        item["sources"].append(source)
    if action and action not in item["actions"]:
        item["actions"].append(action[:500])
    if context and context not in item["contexts"]:
        item["contexts"].append(context)
    item["sources"] = item["sources"][:60]
    item["actions"] = item["actions"][:60]
    item["contexts"] = item["contexts"][:30]
    print(f"FOUND_SID={sid} ACTION={action} SOURCE={source[:180]}", flush=True)


def scan_once(value: object, source: str, action: str | None = None) -> None:
    text = str(value or "")
    for pattern in MATTERPORT_URL_PATTERNS:
        for match in pattern.finditer(text):
            add_sid(
                match.group(1),
                source,
                text[max(0, match.start() - 350) : min(len(text), match.end() + 500)],
                action,
            )
    try:
        parsed = urlparse(text)
        if "matterport.com" in parsed.netloc.lower():
            for sid in parse_qs(parsed.query).get("m", []):
                add_sid(sid, source, text, action)
    except Exception:
        pass


def scan(value: object, source: str, action: str | None = None) -> None:
    text = str(value or "")
    variants = [text, html.unescape(text).replace("\\u0026", "&").replace("\\/", "/")]
    current = variants[-1]
    for _ in range(4):
        try:
            decoded = unquote(current)
        except Exception:
            break
        if decoded == current:
            break
        variants.append(decoded)
        current = decoded
    for index, variant in enumerate(variants):
        scan_once(variant, f"{source}:variant-{index}", action)


async def settle() -> None:
    if pending_tasks:
        await asyncio.gather(*list(pending_tasks), return_exceptions=True)


async def attach_page(page: Page, label: str) -> None:
    def on_request(request: Any) -> None:
        scan(request.url, f"{label}:request")
        if "matterport" in request.url.lower():
            network_log.append(
                {
                    "kind": "request",
                    "url": request.url[:3000],
                    "action": state["action"],
                }
            )

    def on_frame(frame: Any) -> None:
        scan(frame.url, f"{label}:frame")
        if "matterport" in frame.url.lower():
            network_log.append(
                {
                    "kind": "frame",
                    "url": frame.url[:3000],
                    "action": state["action"],
                }
            )

    async def consume_response(response: Any) -> None:
        try:
            scan(response.url, f"{label}:response")
            content_type = (response.headers.get("content-type") or "").lower()
            content_length = int(response.headers.get("content-length") or 0)
            if "matterport" in response.url.lower():
                network_log.append(
                    {
                        "kind": "response",
                        "url": response.url[:3000],
                        "action": state["action"],
                    }
                )
            if not re.search(r"json|javascript|text|html|xml", content_type):
                return
            if content_length > 30_000_000:
                return
            body = await response.text()
            body = body[:30_000_000]
            scan(body, f"{label}:body:{response.url[:260]}")
            if re.search(r"matterport|virtual.?tour|AS2|AT2|\bA5\b|\bB1\b|\bB3\b|\bC1\b", body, re.I):
                digest = str(abs(hash(response.url)))
                Path(OUTPUT / f"response-{digest}.txt").write_text(
                    f"URL: {response.url}\nCONTENT-TYPE: {content_type}\n\n{body}",
                    errors="replace",
                )
        except Exception:
            return

    def on_response(response: Any) -> None:
        task = asyncio.create_task(consume_response(response))
        pending_tasks.add(task)
        task.add_done_callback(lambda completed: pending_tasks.discard(completed))

    page.on("request", on_request)
    page.on("framenavigated", on_frame)
    page.on("response", on_response)


async def scan_page(page: Page, label: str) -> None:
    scan(page.url, f"{label}:page-url")
    for frame in page.frames:
        scan(frame.url, f"{label}:frame-url")
        try:
            values = await frame.evaluate(
                """() => {
                    const out = [document.documentElement?.outerHTML || '', location.href];
                    for (const resource of performance.getEntriesByType('resource')) {
                        if (resource.name) out.push(resource.name);
                    }
                    for (const element of document.querySelectorAll(
                        'iframe,a,button,img,[src],[href],[data-src],[data-url],[data-href],[data-tour],[data-model],[onclick],[aria-label],[title]'
                    )) out.push(element.outerHTML || '');
                    for (const script of document.scripts) {
                        out.push(script.textContent || '', script.src || '');
                    }
                    return out.slice(0, 40000);
                }"""
            )
            for value in values:
                scan(value, f"{label}:dom")
        except Exception:
            continue


async def click_locator(locator: Locator, action: str, page: Page) -> bool:
    state["action"] = action
    try:
        await locator.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        await locator.click(timeout=5000, force=True, no_wait_after=True)
    except Exception:
        try:
            await locator.evaluate("element => element.click()")
        except Exception:
            return False
    await page.wait_for_timeout(3500)
    await settle()
    for open_page in page.context.pages:
        await scan_page(open_page, action)
    return True


async def accept_cookies(page: Page) -> None:
    for pattern in [r"accept all", r"accept", r"agree", r"allow all", r"got it", r"continue"]:
        try:
            controls = await page.get_by_role("button", name=re.compile(pattern, re.I)).all()
            for control in controls[:8]:
                if await control.is_visible():
                    try:
                        await control.click(timeout=1200, force=True)
                    except Exception:
                        pass
        except Exception:
            pass


async def scroll_all(page: Page) -> None:
    try:
        height = min(
            int(
                await page.evaluate(
                    "Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0)"
                )
            ),
            90_000,
        )
        for position in range(0, height, 650):
            await page.evaluate("position => window.scrollTo(0, position)", position)
            await page.wait_for_timeout(75)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def click_named_controls(page: Page, context_label: str) -> None:
    patterns = [
        r"show unavailable floor plans",
        r"7 virtual tours",
        r"virtual tours?",
        r"view virtual tour",
        r"matterport 3d tour",
        r"3d tour",
        r"guided tour",
    ]
    for pattern in patterns:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=re.compile(pattern, re.I))
                count = min(await locator.count(), 30)
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible():
                        await click_locator(
                            item,
                            f"{context_label}:{role}:{pattern}:{index}",
                            page,
                        )
            except Exception:
                pass


async def click_tour_images(page: Page, context_label: str) -> None:
    selectors = [
        "img[alt*='Matterport' i]",
        "img[alt*='Virtual Tour' i]",
        "[aria-label*='Matterport' i]",
        "[title*='Matterport' i]",
        "[data-testid*='virtual' i]",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 100)
            for index in range(count):
                item = locator.nth(index)
                if await item.is_visible():
                    target = item.locator("xpath=ancestor-or-self::button[1] | ancestor-or-self::a[1]")
                    if await target.count():
                        item = target.first
                    await click_locator(item, f"{context_label}:tour-image:{selector}:{index}", page)
        except Exception:
            pass


async def click_plan_cards(page: Page, context_label: str) -> None:
    for plan in PLANS:
        try:
            matches = page.get_by_text(plan, exact=False)
            match_count = min(await matches.count(), 15)
        except Exception:
            match_count = 0
        for match_index in range(match_count):
            heading = matches.nth(match_index)
            state["action"] = f"{context_label}:plan:{plan}:{match_index}"
            try:
                card = heading.locator(
                    "xpath=ancestor::*[.//img[contains(translate(@alt,'MATTERPORT','matterport'),'matterport')] or .//*[contains(translate(normalize-space(text()),'VIRTUALTOUR','virtualtour'),'virtual tour')]][1]"
                )
                targets = card.locator("img[alt*='Matterport' i], button, a, [role=button]")
                target_count = min(await targets.count(), 40)
                for target_index in range(target_count):
                    target = targets.nth(target_index)
                    if not await target.is_visible():
                        continue
                    descriptor = " ".join(
                        filter(
                            None,
                            [
                                await target.get_attribute("alt"),
                                await target.get_attribute("aria-label"),
                                await target.get_attribute("title"),
                                await target.get_attribute("href"),
                                await target.inner_text(timeout=250),
                            ],
                        )
                    )
                    if re.search(r"matterport|virtual.?tour|3d.?tour", descriptor, re.I):
                        await click_locator(
                            target,
                            f"{context_label}:plan:{plan}:{match_index}:{target_index}",
                            page,
                        )
                        break
            except Exception:
                try:
                    clicked = await page.evaluate(
                        """plan => {
                            const candidates = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading],div,span')]
                                .filter(element => (element.textContent || '').includes(plan));
                            for (const candidate of candidates) {
                                let parent = candidate;
                                for (let depth = 0; depth < 12 && parent; depth++, parent = parent.parentElement) {
                                    const image = parent.querySelector('img[alt*="Matterport" i]');
                                    if (image) {
                                        (image.closest('button,a,[role=button]') || image).click();
                                        return true;
                                    }
                                    const controls = [...parent.querySelectorAll('button,a,[role=button]')];
                                    const target = controls.find(element => /matterport|virtual.?tour|3d.?tour/i.test(
                                        (element.textContent || '') + ' ' +
                                        (element.getAttribute('aria-label') || '') + ' ' +
                                        (element.getAttribute('title') || '')
                                    ));
                                    if (target) {
                                        target.click();
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }""",
                        plan,
                    )
                    if clicked:
                        await page.wait_for_timeout(3500)
                        await settle()
                        for open_page in page.context.pages:
                            await scan_page(open_page, state["action"])
                except Exception:
                    pass
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass


async def traverse_gallery(page: Page, context_label: str) -> None:
    for step in range(24):
        state["action"] = f"{context_label}:gallery:{step}"
        for open_page in page.context.pages:
            await scan_page(open_page, state["action"])
        advanced = False
        for pattern in [r"next", r"right", r"forward", r"next image", r"next media", r"next slide"]:
            try:
                controls = page.get_by_role("button", name=re.compile(pattern, re.I))
                count = min(await controls.count(), 12)
                for index in range(count):
                    control = controls.nth(index)
                    if await control.is_visible():
                        advanced = await click_locator(
                            control,
                            f"{context_label}:gallery:{step}:{pattern}:{index}",
                            page,
                        )
                        if advanced:
                            break
            except Exception:
                pass
            if advanced:
                break
        if not advanced:
            try:
                await page.keyboard.press("ArrowRight")
                await page.wait_for_timeout(2200)
            except Exception:
                pass


async def crawl_cyan_detail_pages(page: Page, context: BrowserContext) -> None:
    try:
        links = await page.eval_on_selector_all(
            "a[href]",
            """elements => elements.map(element => ({
                href: element.href,
                text: (element.textContent || '').trim(),
                title: element.title || '',
                aria: element.getAttribute('aria-label') || ''
            }))""",
        )
    except Exception:
        links = []
    detail_urls: list[str] = []
    for item in links:
        href = item.get("href", "")
        descriptor = " ".join(str(item.get(key, "")) for key in ("href", "text", "title", "aria"))
        if "/floorplans/" not in href:
            continue
        if not re.search(r"AS2|A5|AT2|B1|B3|C1|Studio|Bedroom|Townhome", descriptor, re.I):
            continue
        if href not in detail_urls:
            detail_urls.append(href)
    print("CYAN_DETAIL_URLS=" + json.dumps(detail_urls), flush=True)
    for index, url in enumerate(detail_urls[:40]):
        child = await context.new_page()
        await attach_page(child, f"cyan-detail-{index}")
        state["action"] = f"cyan-detail:{url}"
        try:
            await child.goto(url, wait_until="domcontentloaded", timeout=120_000)
            await child.wait_for_timeout(6500)
            await accept_cookies(child)
            await scroll_all(child)
            await scan_page(child, state["action"])
            await click_named_controls(child, state["action"])
            await click_tour_images(child, state["action"])
            await traverse_gallery(child, state["action"])
            await scan_page(child, f"{state['action']}:final")
        except Exception as error:
            print(f"CYAN_DETAIL_ERROR={url} ERROR={str(error)[:500]}", flush=True)
        try:
            await child.screenshot(path=str(OUTPUT / f"cyan-detail-{index}.png"), full_page=True)
        except Exception:
            pass
        await child.close()


async def crawl_source(context: BrowserContext, url: str, index: int) -> dict[str, Any]:
    page = await context.new_page()
    label = f"source-{index}"
    await attach_page(page, label)
    report: dict[str, Any] = {"url": url, "finalUrl": None, "title": None, "error": None}
    state["action"] = f"navigate:{url}"
    print(f"NAVIGATE={url}", flush=True)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(8000)
        await accept_cookies(page)
        await scroll_all(page)
        await page.wait_for_timeout(2500)
        await scan_page(page, f"{label}:initial")
        if "cyanpdx.com" in url:
            await crawl_cyan_detail_pages(page, context)
        await click_named_controls(page, label)
        await click_plan_cards(page, label)
        await click_tour_images(page, label)
        await traverse_gallery(page, label)
        await settle()
        await scan_page(page, f"{label}:final")
        report["finalUrl"] = page.url
        report["title"] = await page.title()
        try:
            Path(OUTPUT / f"source-{index}.html").write_text(
                await page.content(), errors="replace"
            )
        except Exception:
            pass
        try:
            await page.screenshot(path=str(OUTPUT / f"source-{index}.png"), full_page=True)
        except Exception:
            pass
    except Exception as error:
        report["error"] = str(error)[:2500]
        print(f"SOURCE_ERROR={url} ERROR={report['error']}", flush=True)
    await page.close()
    return report


def validate_candidates() -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    for sid, item in records.items():
        url = f"https://my.matterport.com/api/v1/player/models/{sid}/thumb"
        try:
            response = session.get(url, timeout=25, allow_redirects=True)
            item["validationUrl"] = response.url
            item["validationStatus"] = response.status_code
            item["validationContentType"] = response.headers.get("content-type")
            item["validPublicModel"] = response.status_code == 200 and (
                "image" in (response.headers.get("content-type") or "").lower()
                or len(response.content) > 2000
            )
        except Exception as error:
            item["validationError"] = str(error)
            item["validPublicModel"] = False


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1300},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        async def popup_handler(page: Page) -> None:
            await attach_page(page, "popup")

        context.on("page", lambda page: asyncio.create_task(popup_handler(page)))
        reports = []
        for index, url in enumerate(SOURCES, start=1):
            reports.append(await crawl_source(context, url, index))
            valid_count = sum(
                1 for item in records.values() if item.get("validPublicModel") is not False
            )
            if len(records) >= 7 and valid_count >= 7:
                break
        await settle()
        await context.close()
        await browser.close()

    add_sid(KNOWN_FITNESS, "known-public-fitness-tour", "Cyan PDX Fitness Center", "known")
    validate_candidates()
    valid_records = [
        item for item in records.values() if item.get("validPublicModel") is True
    ]
    result = {
        "generatedAt": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "sources": reports,
        "models": sorted(records.values(), key=lambda item: item["modelSid"]),
        "validModels": sorted(valid_records, key=lambda item: item["modelSid"]),
        "candidateCount": len(records),
        "validModelCount": len(valid_records),
        "expectedTourCount": 7,
        "expectedPlans": PLANS,
    }
    (OUTPUT / "models.json").write_text(json.dumps(result, indent=2) + "\n")
    (OUTPUT / "models.txt").write_text(
        "\n".join(item["modelSid"] for item in result["validModels"]) + "\n"
    )
    (OUTPUT / "network.json").write_text(json.dumps(network_log, indent=2) + "\n")
    print("DISCOVERY_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
    print(f"VALID_MODEL_COUNT={len(valid_records)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
