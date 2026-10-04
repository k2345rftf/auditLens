"""Ad-hoc диагностика вкладки «Лазейки»: весь видимый текст фрейма + где «Уязвимост»."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 1200})
    page.goto("http://127.0.0.1:8000/", wait_until="load")
    page.wait_for_timeout(5000)
    page.get_by_role("button", name="Лазейки").first.click()
    page.wait_for_timeout(6000)
    frames = [f for f in page.frames if "loophole.html" in (f.url or "")]
    with open("workspace/ui_diag.txt", "w", encoding="utf-8") as f:
        f.write(f"frames: {[fr.url for fr in frames]}\n")
        if frames:
            text = frames[0].locator("body").inner_text()
            f.write("=== IFRAME TEXT ===\n")
            f.write(text[:3000])
            f.write("\n=== «Уязвимост» контексты ===\n")
            for line in text.splitlines():
                if "язвимост" in line.lower():
                    f.write(f"  {line}\n")
    page.screenshot(path="workspace/loophole_tab.png", full_page=True)
    b.close()
print("done")
