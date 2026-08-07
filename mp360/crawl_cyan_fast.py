#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from playwright.async_api import BrowserContext, Locator, Page, async_playwright

URL = "https://www.apartments.com/cyan-pdx-portland-or/hjsnmt2/"
OUT = Path("work/discovery")
OUT.mkdir(parents=True, exist_ok=True)
KNOWN_FITNESS = "i1MqSM99sWw"
VALID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PATTERNS = [
    re.compile(
        r"(?:https?:)?//(?:my\.)?matterport\.com/(?:show|discover)/?\?[^\s\"'<>]{0,1800}?[?&]m=([A-Za-z0-9_-]{11})",
        re.I,
    ),
    re.compile(r"my\.matterport\.com/api/v1/player/models/([A-Za-z0-9_-]{11})", re.I),
    re.compile(r"my\.matterport\.com/(?:models|spaces)/([A-Za-z0-9_-]{11})", re.I),
    re.compile(
        r"/services/3d-tours/[A-Za-z0-9_-]+/([A-Za-z0-9_-]{11})(?:[/?#\"' ]|$)",
        re.I,
    ),
    re.compile(
        r"[\"'](?:modelSid|modelId|matterportModelId|matterportId|matterport_id|spaceSid|spaceId|tourId)[\"']\s*[:=]\s*[\"']([A-Za-z0-9_-]{11})[\"']",
        re.I,
    ),
]

hits: dict[str, dict] = {}
pending: set[asyncio.Task] = set()
state = {"action": "startup"}


def add(sid: object, source: str, context: str = "") -> None:
    sid = str(sid or "").strip()
    if not VALID.fullmatch(sid):
        return
    item = hits.setdefault(
        sid,
        {"modelSid": sid, "sources": [], "actions": [], "contexts": []},
    )
    source = str(source)[:500]
    context = re.sub(r"\s+", " ", str(context))[:1200]
    if source not in item["sources"]:
        item["sources"].append(source)
    if state["action"] not in item["actions"]:
        item["actions"].append(state["action"][:500])
    if context and context not in item["contexts"]:
        item["contexts"].append(context)
    item["sources"] = item["sources"][:50]
    item["actions"] = item["actions"][:50]
    item["contexts"] = item["contexts"][:25]
    print(
        f"FAST_FOUND_SID={sid} ACTION={state['action']} SOURCE={source[:180]}",
        flush=True,
    )


def scan_once(value: object, source: str) -> None:
    text = str(value or "")
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            add(
                match.group(1),
                source,
                text[max(0, match.start() - 320) : min(len(text), match.end() + 500)],
            )
    try:
        parsed = urlparse(text)
        if "matterport.com" in parsed.netloc.lower():
            for sid in parse_qs(parsed.query).get("m", []):
                add(sid, source, text)
    except Exception:
        pass


def scan(value: object, source: str) -> None:
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
        scan_once(variant, f"{source}:v{index}")


async def settle(milliseconds: int = 1500) -> None:
    await asyncio.sleep(milliseconds / 1000)
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)


async def attach(page: Page, label: str) -> None:
    page.on("request", lambda request: scan(request.url, f"{label}:request"))
    page.on("framenavigated", lambda frame: scan(frame.url, f"{label}:frame"))

    async def consume(response: object) -> None:
        try:
            scan(response.url, f"{label}:response")
            content_type = (response.headers.get("content-type") or "").lower()
            content_length = int(response.headers.get("content-length") or 0)
            if not re.search(r"json|javascript|text|html|xml", content_type):
                return
            if content_length > 25_000_000:
                return
            body = await response.text()
            scan(body[:25_000_000], f"{label}:body:{response.url[:260]}")
        except Exception:
            pass

    def on_response(response: object) -> None:
        task = asyncio.create_task(consume(response))
        pending.add(task)
        task.add_done_callback(lambda finished: pending.discard(finished))

    page.on("response", on_response)


async def inspect(page: Page, label: str) -> None:
    scan(page.url, f"{label}:url")
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
                        'iframe,a,button,img,[src],[href],[data-src],[data-url],[data-href],[data-tour],[data-tour-id],[data-model],[data-model-id],[onclick],[aria-label],[title]'
                    )) out.push(element.outerHTML || '');
                    return out.slice(0, 30000);
                }"""
            )
            for value in values:
                scan(value, f"{label}:dom")
        except Exception:
            pass


async def click(locator: Locator, action: str, page: Page) -> bool:
    state["action"] = action
    try:
        await locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        await locator.click(timeout=4500, force=True, no_wait_after=True)
    except Exception:
        try:
            await locator.evaluate("element => element.click()")
        except Exception:
            return False
    await settle(3500)
    for open_page in page.context.pages:
        await inspect(open_page, action)
    return True


async def close_overlay(page: Page) -> None:
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)
    except Exception:
        pass
    for pattern in [r"close", r"close dialog", r"close gallery"]:
        try:
            buttons = page.get_by_role("button", name=re.compile(pattern, re.I))
            for index in range(min(await buttons.count(), 6)):
                button = buttons.nth(index)
                if await button.is_visible():
                    try:
                        await button.click(timeout=1000, force=True)
                    except Exception:
                        pass
        except Exception:
            pass


async def accept(page: Page) -> None:
    for pattern in [r"accept all", r"accept", r"agree", r"allow all", r"got it"]:
        try:
            buttons = page.get_by_role("button", name=re.compile(pattern, re.I))
            for index in range(min(await buttons.count(), 8)):
                button = buttons.nth(index)
                if await button.is_visible():
                    try:
                        await button.click(timeout=1000, force=True)
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
            70_000,
        )
        for position in range(0, height, 650):
            await page.evaluate("position => window.scrollTo(0, position)", position)
            await asyncio.sleep(0.055)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def click_card_tours(page: Page) -> None:
    images = page.locator("img[alt*='Matterport 3D Tour' i]")
    count = min(await images.count(), 30)
    print(f"FAST_MATTERPORT_CARD_COUNT={count}", flush=True)
    for index in range(count):
        if len(hits) >= 7:
            break
        image = images.nth(index)
        if not await image.is_visible():
            continue
        label = ""
        try:
            label = await image.evaluate(
                """element => {
                    let parent = element;
                    for (let depth = 0; depth < 14 && parent; depth++, parent = parent.parentElement) {
                        const text = (parent.innerText || '').replace(/\s+/g, ' ').slice(0, 1200);
                        const match = text.match(/Studio \(AS2\)|One Bedroom \(A5\)|Large One Bedroom Townhome \(AT2\)|Two Bedroom, One Bath \(B1\)|Two Bedroom, Two Bath \(B3\)|Three Bedroom \(C1\)|Fitness Center/i);
                        if (match) return match[0];
                    }
                    return '';
                }"""
            )
        except Exception:
            pass
        target = image.locator("xpath=ancestor::button[1] | ancestor::a[1] | ancestor::*[@role='button'][1]")
        if await target.count():
            image = target.first
        await click(image, f"card:{index}:{label or 'unknown'}", page)
        await close_overlay(page)


async def click_top_gallery(page: Page) -> None:
    for pattern in [r"7 virtual tours", r"matterport 3d tours", r"virtual tour"]:
        if len(hits) >= 7:
            break
        try:
            controls = page.get_by_role("button", name=re.compile(pattern, re.I))
            for index in range(min(await controls.count(), 10)):
                control = controls.nth(index)
                if await control.is_visible():
                    await click(control, f"open-gallery:{pattern}:{index}", page)
                    break
        except Exception:
            pass

    for step in range(12):
        if len(hits) >= 7:
            break
        state["action"] = f"gallery:{step}"
        for open_page in page.context.pages:
            await inspect(open_page, state["action"])
        advanced = False
        for pattern in [r"next image", r"carousel next", r"next", r"right", r"forward"]:
            try:
                controls = page.get_by_role("button", name=re.compile(pattern, re.I))
                for index in range(min(await controls.count(), 8)):
                    control = controls.nth(index)
                    if await control.is_visible():
                        advanced = await click(
                            control,
                            f"gallery:{step}:{pattern}:{index}",
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
                await settle(1800)
            except Exception:
                pass


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1600, "height": 1350},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        context.on("page", lambda page: asyncio.create_task(attach(page, "popup")))
        page = await context.new_page()
        await attach(page, "apartments")
        state["action"] = "navigate"
        await page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(7)
        await accept(page)
        await scroll_all(page)
        await inspect(page, "initial")

        try:
            show = page.get_by_role("button", name=re.compile(r"show unavailable floor plans", re.I))
            for index in range(min(await show.count(), 12)):
                button = show.nth(index)
                if await button.is_visible():
                    await click(button, f"show-unavailable:{index}", page)
        except Exception:
            pass

        await click_card_tours(page)
        await click_top_gallery(page)
        await settle(2500)
        await inspect(page, "final")
        try:
            await page.screenshot(path=str(OUT / "fast-apartments.png"), full_page=True)
        except Exception:
            pass
        await context.close()
        await browser.close()

    add(KNOWN_FITNESS, "known-fitness", "Fitness Center")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    valid = []
    for item in hits.values():
        sid = item["modelSid"]
        try:
            response = session.get(
                f"https://my.matterport.com/api/v1/player/models/{sid}/thumb",
                timeout=25,
                allow_redirects=True,
            )
            item["validationStatus"] = response.status_code
            item["validationUrl"] = response.url
            item["validationContentType"] = response.headers.get("content-type")
            item["validPublicModel"] = response.status_code == 200 and (
                "image" in (response.headers.get("content-type") or "").lower()
                or len(response.content) > 2000
            )
        except Exception as error:
            item["validPublicModel"] = False
            item["validationError"] = str(error)
        if item["validPublicModel"]:
            valid.append(item)

    result = {
        "candidateCount": len(hits),
        "validModelCount": len(valid),
        "models": sorted(hits.values(), key=lambda item: item["modelSid"]),
        "validModels": sorted(valid, key=lambda item: item["modelSid"]),
    }
    (OUT / "models.json").write_text(json.dumps(result, indent=2) + "\n")
    (OUT / "models.txt").write_text(
        "\n".join(item["modelSid"] for item in result["validModels"]) + "\n"
    )
    print("FAST_DISCOVERY_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
    if len(valid) < 7:
        raise SystemExit(f"Fast resolver found {len(valid)} of 7 public models")


if __name__ == "__main__":
    asyncio.run(main())
