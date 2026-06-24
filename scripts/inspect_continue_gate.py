"""
Diagnostic script — figure out what the IG 'Continue as <user>' Continue
button actually looks like, so we can write a working selector.

Loads the burner session, navigates somewhere that triggers the gate,
dumps the surrounding HTML to disk for manual inspection, and lists
every element that has 'Continue' text.

Usage:
    python scripts/inspect_continue_gate.py --handle ignorethisdump2

Outputs (to /tmp/ or /home/claude/ depending on environment):
  - continue_gate.html        full page HTML
  - continue_gate.png         screenshot
  - continue_candidates.txt   each matching element with its tag/attrs

Read continue_candidates.txt to see what the real Continue button is.
Then update SELECTORS["..."] or _dismiss_continue_gate accordingly.
"""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from app.modules.m7_conversation.io_dm import _ig_session


OUT_DIR = Path("/app/ig_sessions/_debug")


async def inspect(handle: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with _ig_session(handle) as (_ctx, page):
        # Trigger the Continue gate by navigating somewhere that requires auth.
        # Direct thread URL is reliable — it always redirects to /accounts/login/
        # when the session is in this half-authed state.
        target = "https://www.instagram.com/direct/inbox/"
        print(f"Navigating to {target} to trigger the gate...")
        await page.goto(target, timeout=20_000, wait_until="domcontentloaded")
        await asyncio.sleep(3)  # let JS settle

        current_url = page.url
        print(f"Current URL after navigation: {current_url}")

        # Save the full rendered HTML.
        html = await page.content()
        html_path = OUT_DIR / "continue_gate.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"Saved {len(html):,} bytes of HTML to {html_path}")

        # Screenshot for visual confirmation.
        shot_path = OUT_DIR / "continue_gate.png"
        await page.screenshot(path=str(shot_path), full_page=True)
        print(f"Saved screenshot to {shot_path}")

        # Find every element on the page whose text contains 'Continue'.
        # We use JS evaluation since Playwright's :has-text() is fuzzy.
        candidates = await page.evaluate(
            """
            () => {
              const matches = [];
              const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_ELEMENT
              );
              let node;
              while ((node = walker.nextNode())) {
                const t = (node.innerText || '').trim();
                if (t && t.toLowerCase().includes('continue') && t.length < 100) {
                  matches.push({
                    tag: node.tagName,
                    role: node.getAttribute('role'),
                    type: node.getAttribute('type'),
                    classes: node.className && node.className.toString().slice(0, 120),
                    id: node.id,
                    text: t.slice(0, 80),
                    outerHTML: node.outerHTML.slice(0, 600),
                    rect: (() => {
                      const r = node.getBoundingClientRect();
                      return { x: r.x, y: r.y, w: r.width, h: r.height };
                    })(),
                  });
                }
              }
              return matches;
            }
            """
        )

        candidates_path = OUT_DIR / "continue_candidates.txt"
        with candidates_path.open("w", encoding="utf-8") as f:
            f.write(f"# Inspecting Continue-gate for @{handle}\n")
            f.write(f"# URL at inspection time: {current_url}\n")
            f.write(f"# Found {len(candidates)} elements containing 'Continue'\n")
            f.write("=" * 80 + "\n\n")
            for i, c in enumerate(candidates):
                f.write(f"[{i}] <{c['tag']}>")
                if c["role"]:
                    f.write(f"  role='{c['role']}'")
                if c["type"]:
                    f.write(f"  type='{c['type']}'")
                if c["id"]:
                    f.write(f"  id='{c['id']}'")
                f.write("\n")
                f.write(f"     text: '{c['text']}'\n")
                f.write(f"     rect: {c['rect']}\n")
                if c["classes"]:
                    f.write(f"     classes: {c['classes']}\n")
                f.write(f"     html: {c['outerHTML']}\n\n")
        print(f"Saved {len(candidates)} candidate(s) to {candidates_path}")

        # Print the most likely-actionable candidate to console.
        clickable = [
            c for c in candidates
            if c["tag"] in ("BUTTON", "A")
            or c["role"] == "button"
            or c["type"] == "submit"
        ]
        if clickable:
            print("\nMost likely actionable Continue elements:")
            for c in clickable[:5]:
                print(f"  <{c['tag']}> role={c['role']} type={c['type']} "
                      f"text='{c['text']}'")
                print(f"    html: {c['outerHTML'][:300]}")
        else:
            print("\n⚠️  No <button> / role='button' / type='submit' "
                  "Continue elements found. IG may be using a custom widget.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--handle", required=True)
    args = p.parse_args()
    asyncio.run(inspect(args.handle))


if __name__ == "__main__":
    main()
