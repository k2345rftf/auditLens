"""Smoke-test demo-режима: запускает реальный браузер, шлёт вопрос,
ждёт готовности отчёта, делает скриншоты для проверки качества.
"""
from __future__ import annotations
import asyncio
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
URL = "http://127.0.0.1:8000"
Q = "Сравни условия оформления и тарифы по нотариальным доверенностям для распоряжения банковским счётом в Сбербанке, ВТБ, Альфа-банке и Тинькофф"


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900},
                                         device_scale_factor=2)
        page = await ctx.new_page()
        print(f"→ {URL}")
        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)
        # Перейти в ИИ-аналитик
        await page.get_by_text("ИИ-аналитик").first.click()
        await page.wait_for_timeout(1200)
        # Ввести вопрос
        await page.wait_for_selector("textarea.chat-textarea", timeout=10000)
        ta = page.locator("textarea.chat-textarea").first
        await ta.click()
        await ta.fill(Q)
        await page.wait_for_timeout(400)
        # Включить Deep Research
        await page.get_by_role("button", name="Deep Research").first.click()
        await page.wait_for_timeout(300)
        # Отправить
        await ta.press("Enter")
        print("→ запрос отправлен, ждём...")

        # Снимок процесса через 8 сек (discovery)
        await page.wait_for_timeout(8000)
        await page.screenshot(path=str(OUT / "demo_8s_process.png"), full_page=False)
        print("  ✅ demo_8s_process.png (8s — discovery)")

        # Ждём пока phase=done (или 35s timeout)
        for _ in range(20):
            await page.wait_for_timeout(2000)
            try:
                # Когда loading=false — кнопка отправки переходит из disabled
                # и появляется PdfExportButton в .dr-doc-toolbar
                has_pdf = await page.locator(".btn-export").count()
                if has_pdf > 0:
                    break
            except Exception: pass

        # Скролл наверх — показать начало отчёта с цитатами и графиком
        feed = page.locator(".chat-feed")
        await page.evaluate("document.querySelector('.chat-feed')?.scrollTo({top:0})")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "demo_report_top.png"))
        print("  ✅ demo_report_top.png (начало отчёта)")

        # Скролл в центр (где [[CHART:0]] inline)
        await page.evaluate("document.querySelector('.chat-feed')?.scrollTo({top:600})")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "demo_report_chart.png"))
        print("  ✅ demo_report_chart.png (отчёт с inline-графиком)")

        # Скролл в конец — источники + PDF-кнопка
        await page.evaluate("document.querySelector('.chat-feed')?.scrollTo({top:1e9})")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "demo_report_end.png"))
        print("  ✅ demo_report_end.png (конец отчёта)")

        # Полностраничный
        await page.screenshot(path=str(OUT / "demo_fullpage.png"), full_page=True)
        print("  ✅ demo_fullpage.png (full-page снимок всего отчёта)")

        # Проверить наличие PDF-кнопки
        pdf_btn = page.get_by_role("button", name="Download PDF")
        if await pdf_btn.count() > 0:
            print(f"  ✅ Кнопка Download PDF найдена ({await pdf_btn.count()} шт.)")
        else:
            # Может с другой надписью
            buttons_with_pdf = page.locator("button:has-text('PDF')")
            cnt = await buttons_with_pdf.count()
            print(f"  ⚠ Кнопок с 'PDF' в тексте: {cnt}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
