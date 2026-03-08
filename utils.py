"""
utils.py
--------
Shared browser automation utilities used by all platform posting classes.

Human-like behavior:
  - human_like_type()        → types text with realistic per-character delays
  - human_like_mouse_move()  → moves the mouse along a Bezier curve before clicking
  - get_element_center()     → returns (x, y) center of a DOM element

Account management:
  - deactivate_account()     → sets is_active=False in social_accounts after bot challenge
"""

import asyncio
import random
import math
import logging

logger = logging.getLogger(__name__)

# Injected by app.py
_supabase = None


def init_utils(supabase_client):
    global _supabase
    _supabase = supabase_client


# ─────────────────────────────────────────────────────────────────────────────
# Human-like typing
# ─────────────────────────────────────────────────────────────────────────────

async def human_like_type(element, text: str) -> None:
    """
    Type text into a Playwright element one character at a time.

    Delay profile (mirrors comment-bot):
      - Between characters:   220–320 ms
      - After a space:        400–1200 ms (end of word)
      - After punctuation:    800–2500 ms
      - Pre-typing pause:     800–2000 ms (before first character)
      - Post-typing review:   2500–6000 ms (after last character)
      - Typo + backspace:     7% chance per character
    """
    await asyncio.sleep(random.uniform(0.8, 2.0))  # hesitation before starting

    for i, char in enumerate(text):
        # Occasional typo simulation
        if random.random() < 0.07 and char.isalpha():
            wrong = random.choice("qwertyuiopasdfghjklzxcvbnm")
            await element.type(wrong)
            await asyncio.sleep(random.uniform(0.15, 0.35))
            await element.press("Backspace")
            await asyncio.sleep(random.uniform(0.1, 0.25))

        await element.type(char)

        if char in ".!?":
            await asyncio.sleep(random.uniform(0.8, 2.5))
        elif char == " ":
            await asyncio.sleep(random.uniform(0.4, 1.2))
        else:
            await asyncio.sleep(random.uniform(0.22, 0.32))

    await asyncio.sleep(random.uniform(2.5, 6.0))  # review pause


# ─────────────────────────────────────────────────────────────────────────────
# Human-like mouse movement
# ─────────────────────────────────────────────────────────────────────────────

async def get_element_center(page, selector: str) -> tuple[float, float]:
    """Return the (x, y) center coordinates of the first matching element."""
    element = page.locator(selector).first
    box = await element.bounding_box()
    if box is None:
        raise ValueError(f"Element not visible or off-screen: {selector}")
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


async def human_like_mouse_move(page, target_x: float, target_y: float) -> None:
    """
    Move the mouse to (target_x, target_y) along a quadratic Bezier curve.

    Behaviour:
      - Moves in 30 micro-steps with variable speed (slow at start/end)
      - 30% chance of slight overshoot followed by correction
      - Pauses 100–400 ms before clicking
    """
    current = await page.evaluate("() => ({ x: window.innerWidth / 2, y: window.innerHeight / 2 })")
    start_x, start_y = current["x"], current["y"]

    # Random control point for the Bezier curve
    ctrl_x = (start_x + target_x) / 2 + random.uniform(-80, 80)
    ctrl_y = (start_y + target_y) / 2 + random.uniform(-80, 80)

    steps = 30
    for i in range(steps + 1):
        t = i / steps
        # Ease-in-out: slow at start and end
        t_eased = t * t * (3 - 2 * t)
        x = (1 - t_eased) ** 2 * start_x + 2 * (1 - t_eased) * t_eased * ctrl_x + t_eased ** 2 * target_x
        y = (1 - t_eased) ** 2 * start_y + 2 * (1 - t_eased) * t_eased * ctrl_y + t_eased ** 2 * target_y
        await page.mouse.move(x, y)
        await asyncio.sleep(0.01 + random.random() * 0.02)

    # Overshoot simulation
    if random.random() < 0.30:
        overshoot_x = target_x + random.uniform(-15, 15)
        overshoot_y = target_y + random.uniform(-15, 15)
        await page.mouse.move(overshoot_x, overshoot_y)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.move(target_x, target_y)

    await asyncio.sleep(random.uniform(0.1, 0.4))


# ─────────────────────────────────────────────────────────────────────────────
# Account management
# ─────────────────────────────────────────────────────────────────────────────

def deactivate_account(username: str, platform: str) -> None:
    """
    Mark an account as inactive in Supabase after bot challenge detection.
    The account will be skipped by pre-flight validation on future campaigns
    until manually re-enabled.
    """
    try:
        _supabase.table("social_accounts") \
            .update({"is_active": False}) \
            .eq("username", username) \
            .eq("platform", platform) \
            .execute()
        logger.warning(f"Account deactivated due to bot challenge: @{username} ({platform})")
    except Exception as e:
        logger.error(f"Failed to deactivate account @{username}: {e}")
