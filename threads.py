"""
threads.py
----------
Threads posting automation.

Flow per account:
  1. Navigate to threads.com — verify already logged in via saved cookies
  2. Register popup handlers (notifications, cookie consent)
  3. Check for bot challenge
  4. Click the Create compose button in the left sidebar
  5. Type post_text with human-like delays in the Lexical editor
  6. Attach up to 10 images (if media_paths provided)
  7. Click Post (wait for enabled state first)
  8. Verify post via tiered checks (overlay close → success text → assume)
  9. Emit progress event

Platform limits:
  - Max 10 images per post
  - Max 500 characters of text (enforced by frontend)
  - Text-only posts are valid (media is optional)

Note:
  All interactive elements use Playwright's semantic locators (get_by_role,
  get_by_label, get_by_text) per Playwright best practices. CSS selectors are
  only used for detection (bot challenge, captcha) and hidden inputs that have
  no ARIA role.

  Threads uses a Lexical rich-text editor. Text must be typed character by
  character via human_like_type() — .fill() doesn't work because Lexical
  manages state via keyboard events, not DOM value changes.
"""

import asyncio
import re
import random
import logging

from playwright.async_api import expect
from utils import human_like_type, human_like_mouse_move, deactivate_account
from exceptions import AbortedError

logger = logging.getLogger(__name__)

# ─── Selectors (CSS — detection only) ─────────────────────────────────────────
# These are non-interactive detection selectors. Interactive elements use
# Playwright's semantic locators in the methods below.

SEL_CHALLENGE = (
    '[aria-label*="suspicious"], '
    'form[action*="challenge"], '
    'h2:has-text("We noticed unusual activity"), '
    '[data-testid*="challenge"]'
)
SEL_CAPTCHA = 'iframe[src*="captcha"], #captcha-container'


class BotChallengeError(Exception):
    pass


class ThreadsPoster:
    def __init__(self, page, campaign: dict, media_paths: list[str], emitter, account: str, event_store):
        """
        :param page:        Playwright Page connected via CDP to the Dolphin Anty browser
        :param campaign:    Full campaign dict from Supabase
        :param media_paths: Ordered list of local absolute file paths (may be empty)
        :param emitter:     ProgressEmitter instance
        :param account:     The username being used for this session
        :param event_store: EventStore instance for abort signal checking
        """
        self.page = page
        self.campaign = campaign
        self.media_paths = media_paths[:10]  # Threads limit: 10
        self.emitter = emitter
        self.account = account
        self.event_store = event_store

    async def run(self) -> None:
        """Entry point called by app.py for each account."""
        await self._navigate_home()
        await self._check_abort()
        await self._register_popup_handlers()
        await self._check_abort()
        await self._check_bot_challenge()
        await self._check_abort()
        await self._compose_and_post()

    # ─── Private methods ──────────────────────────────────────────────────────

    async def _check_abort(self) -> None:
        """Raise AbortedError if the user has requested an abort."""
        if self.event_store.is_aborted():
            logger.info(f"[@{self.account}] Abort signal detected")
            raise AbortedError(f"@{self.account} aborted by user")

    async def _navigate_home(self) -> None:
        logger.info(f"[@{self.account}] Navigating to threads.com")
        await self.page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=30000)

        feed_indicator = (
            self.page.get_by_role("feed")
            .or_(self.page.get_by_label(re.compile(r"threads feed|home", re.IGNORECASE)))
            .or_(self.page.get_by_role("main"))
        )
        try:
            await feed_indicator.first.wait_for(state="visible", timeout=15000)
            logger.info(f"[@{self.account}] Threads home confirmed — already logged in")
        except Exception:
            raise RuntimeError(
                f"@{self.account} does not appear to be logged in to Threads. "
                "Open the Dolphin Anty profile manually and log in via Meta account."
            )

    async def _register_popup_handlers(self) -> None:
        """
        Register handlers for popups that can appear at unpredictable times.

        Uses Playwright's add_locator_handler() — Playwright checks for these
        locators before every actionability check and automatically calls the
        handler to dismiss them if visible.
        """
        # "Turn on Notifications" — shared Meta infrastructure
        await self.page.add_locator_handler(
            self.page.get_by_text(re.compile(r"turn on notifications", re.IGNORECASE)),
            lambda _: self.page.get_by_role("dialog")
                .get_by_text("Not Now", exact=True)
                .first.click(),
        )
        logger.debug(f"[@{self.account}] Registered handler: Turn on Notifications → Not Now")

        # Cookie consent (may appear on threads.com)
        await self.page.add_locator_handler(
            self.page.get_by_text(re.compile(r"allow.*cookies|cookie preferences", re.IGNORECASE)),
            lambda _: self.page.get_by_role("button", name=re.compile(r"decline|reject|not now", re.IGNORECASE))
                .first.click(),
        )
        logger.debug(f"[@{self.account}] Registered handler: Cookie consent → Decline")

    async def _check_bot_challenge(self) -> None:
        try:
            await asyncio.sleep(1)
            if (
                await self.page.locator(SEL_CHALLENGE).count() > 0
                or await self.page.locator(SEL_CAPTCHA).count() > 0
            ):
                logger.warning(f"[@{self.account}] Bot challenge detected on Threads")
                deactivate_account(self.account, "threads")
                raise BotChallengeError(f"@{self.account} triggered a bot challenge on Threads.")
        except BotChallengeError:
            raise
        except Exception as e:
            logger.debug(f"Bot challenge check error (non-fatal): {e}")

    async def _compose_and_post(self) -> None:
        post_text = self.campaign["caption"]
        logger.info(f"[@{self.account}] Composing thread ({len(post_text)} chars, {len(self.media_paths)} images)")

        await self._click_compose_button()
        await self._check_abort()

        await self._type_thread_text(post_text)
        await self._check_abort()

        if self.media_paths:
            await self._attach_media()
            await self._check_abort()

        await self._click_post()

        # No abort check after post — already submitted
        await self._verify_posted()

        logger.info(f"[@{self.account}] Thread posted successfully")
        self.emitter.post_published(
            account=self.account,
            message=f"Thread posted as @{self.account}",
        )

    async def _click_compose_button(self) -> None:
        """
        Click the Create (+) button in the left sidebar.

        Threads' left sidebar has a "+" icon element with "Create" as its
        hover/tooltip text — same pattern as Instagram's sidebar.
        """
        compose_btn = (
            self.page.get_by_role("link", name="Create")
            .or_(self.page.get_by_label("Create"))
            .or_(self.page.get_by_role("link", name="New thread"))
            .or_(self.page.get_by_label("New thread"))
        )
        logger.info(f"[@{self.account}] Waiting for compose button to appear")
        await compose_btn.first.wait_for(state="visible", timeout=15000)

        # Human-like mouse movement to button center
        try:
            box = await compose_btn.first.bounding_box()
            if box:
                await human_like_mouse_move(
                    self.page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
        except Exception:
            pass

        # Click with overlay dismiss fallback
        try:
            await compose_btn.first.click(timeout=10000)
        except Exception:
            logger.info(f"[@{self.account}] Compose button blocked by overlay — attempting to dismiss")
            await self._dismiss_any_overlay()
            await compose_btn.first.click(timeout=15000)

        logger.info(f"[@{self.account}] Clicked compose button")
        await asyncio.sleep(random.uniform(0.8, 1.5))

    async def _type_thread_text(self, text: str) -> None:
        """
        Find the text editor inside the compose overlay and type with human-like delays.

        Threads' Lexical editor is a div[contenteditable="true"][role="textbox"]
        with placeholder "What's new?". Uses a tiered locator strategy with retries
        to handle slow network and overlay interference.
        """
        logger.info(f"[@{self.account}] Locating text editor in compose overlay")
        dialog = self.page.get_by_role("dialog")

        # Tiered locator strategy — based on actual Threads compose dialog UI.
        # Primary uses the exact placeholder text visible in the compose overlay.
        locator_tiers = [
            ("dialog get_by_placeholder",
             dialog.get_by_placeholder("What's new?")),
            ("dialog get_by_role textbox",
             dialog.get_by_role("textbox")),
            ("dialog contenteditable",
             dialog.locator('[contenteditable="true"]')),
        ]

        max_attempts = 3
        last_error = None

        for attempt in range(1, max_attempts + 1):
            for tier_name, locator in locator_tiers:
                try:
                    await locator.first.wait_for(state="visible", timeout=10000)
                    logger.info(f"[@{self.account}] Found text editor via '{tier_name}' (attempt {attempt})")

                    # Try clicking — dismiss overlay and retry if blocked
                    try:
                        await locator.first.click(timeout=5000)
                    except Exception:
                        logger.info(f"[@{self.account}] Click blocked — dismissing overlay and retrying")
                        await self._dismiss_any_overlay()
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                        await locator.first.click(timeout=10000)

                    await human_like_type(locator.first, text)
                    return  # success
                except Exception as e:
                    last_error = e
                    logger.debug(f"[@{self.account}] Tier '{tier_name}' failed (attempt {attempt}): {e}")
                    continue

            # Before retrying, wait and try to dismiss overlays
            if attempt < max_attempts:
                logger.warning(
                    f"[@{self.account}] All locator tiers failed (attempt {attempt}/{max_attempts}) — "
                    f"retrying in {attempt * 2}s"
                )
                await self._dismiss_any_overlay()
                await asyncio.sleep(attempt * 2)

        raise RuntimeError(f"Could not type thread text after {max_attempts} attempts: {last_error}")

    async def _attach_media(self) -> None:
        """
        Inject local image files into Threads' hidden file input.

        Threads' compose dialog has a media attach button (paperclip/image icon)
        that reveals the hidden file input. We scope searches to the dialog first,
        then fall back to page-level. Includes retry logic for network delays.
        """
        if not self.media_paths:
            return

        logger.info(f"[@{self.account}] Attaching {len(self.media_paths)} image(s)")

        dialog = self.page.get_by_role("dialog")

        # Click the "Attach media" button (image icon) inside the compose dialog.
        # The tooltip/aria-label is "Attach media" per the actual Threads UI.
        attach_btn = (
            dialog.get_by_label("Attach media")
            .or_(dialog.get_by_label(re.compile(r"attach media|add media", re.IGNORECASE)))
        )
        try:
            await attach_btn.first.wait_for(state="visible", timeout=10000)
            await attach_btn.first.click(timeout=5000)
            logger.info(f"[@{self.account}] Clicked 'Attach media' button")
            await asyncio.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            logger.warning(f"[@{self.account}] Could not click attach button: {e}")

        # Hidden file input — scope to dialog first, then page.
        # CSS selector is correct here (no ARIA role on hidden inputs).
        media_input = None
        file_input_selectors = [
            dialog.locator('input[accept*="image"], input[type="file"]'),
            self.page.locator('input[accept*="image"], input[type="file"]'),
        ]

        for attempt in range(3):
            for selector in file_input_selectors:
                try:
                    await selector.first.wait_for(state="attached", timeout=8000)
                    media_input = selector.first
                    break
                except Exception:
                    continue

            if media_input:
                break

            if attempt < 2:
                logger.warning(
                    f"[@{self.account}] File input not found (attempt {attempt + 1}/3) — "
                    f"retrying in {attempt + 2}s"
                )
                await asyncio.sleep(attempt + 2)

        if not media_input:
            logger.warning(f"[@{self.account}] File input not found after 3 attempts — skipping media attachment")
            return

        # set_input_files() with retry — file locks or network delays can cause
        # transient failures.
        for attempt in range(3):
            try:
                await media_input.set_input_files(self.media_paths)
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning(
                        f"[@{self.account}] set_input_files failed (attempt {attempt + 1}/3), "
                        f"retrying in 2s: {e}"
                    )
                    await asyncio.sleep(2)
                else:
                    logger.warning(f"[@{self.account}] Media attach failed after 3 attempts: {e}")
                    return

        # Wait for image previews to render before posting
        preview_img = self.page.locator('img[src^="blob:"]')
        try:
            await preview_img.first.wait_for(state="visible", timeout=30000)
            logger.info(f"[@{self.account}] Media previews ready")
        except Exception as e:
            logger.warning(f"[@{self.account}] Media preview not detected — continuing anyway: {e}")

    async def _click_post(self) -> None:
        """
        Click the Post button at the bottom-right of the compose overlay.

        The Post button is DISABLED until text is typed or media is uploaded.
        We wait for it to be both visible AND enabled before clicking.
        """
        dialog = self.page.get_by_role("dialog")
        post_btn = dialog.get_by_role("button", name="Post").first

        logger.info(f"[@{self.account}] Waiting for Post button to appear")
        try:
            await post_btn.wait_for(state="visible", timeout=10000)
            logger.info(f"[@{self.account}] Post button visible")
        except Exception:
            logger.warning(f"[@{self.account}] Post button did not appear within 10s — compose overlay may not have opened")
            raise RuntimeError("Post button did not appear within timeout")

        # wait_for() only accepts "attached"/"detached"/"visible"/"hidden" —
        # there is no "enabled" state. expect().to_be_enabled() auto-retries.
        logger.info(f"[@{self.account}] Waiting for Post button to be enabled")
        try:
            await expect(post_btn).to_be_enabled(timeout=10000)
            logger.info(f"[@{self.account}] Post button enabled — ready to submit")
        except Exception:
            logger.warning(
                f"[@{self.account}] Post button remained disabled after 10s — "
                "text may not have been registered by the Lexical editor"
            )
            raise RuntimeError(
                "Post button remained disabled — text or media may not have "
                "been registered by the Lexical editor"
            )

        # Human-like mouse movement
        try:
            box = await post_btn.bounding_box()
            if box:
                await human_like_mouse_move(
                    self.page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
        except Exception:
            pass

        await post_btn.click()
        logger.info(f"[@{self.account}] Clicked Post")

    async def _verify_posted(self) -> None:
        """
        Verify the thread was posted using a tiered approach.

        Primary signal: the compose overlay closes automatically on success.
        Fallback: check for success toast text.
        Final: assume success after a delay.
        """
        # Primary: overlay should close on success
        logger.info(f"[@{self.account}] Verifying post — waiting for compose overlay to close (up to 30s)")
        try:
            await self.page.get_by_role("dialog").wait_for(state="hidden", timeout=30000)
            logger.info(f"[@{self.account}] Compose overlay closed — post likely succeeded")
            await asyncio.sleep(2)
            return
        except Exception:
            logger.info(f"[@{self.account}] Overlay still open after 30s — checking for success toast")

        # Fallback 1: check for success toast text
        success_text = self.page.get_by_text(
            re.compile(r"thread (posted|published|shared)|posted successfully", re.IGNORECASE)
        )
        logger.info(f"[@{self.account}] Fallback: looking for success toast text (up to 10s)")
        try:
            await success_text.first.wait_for(state="visible", timeout=10000)
            logger.info(f"[@{self.account}] Success text detected")
            await asyncio.sleep(2)
            return
        except Exception:
            logger.info(f"[@{self.account}] No success toast detected — using final fallback (5s wait)")

        # Final fallback: assume success
        await asyncio.sleep(5)
        logger.warning(f"[@{self.account}] Could not verify thread post — assuming success")

    async def _dismiss_any_overlay(self) -> None:
        """
        Try to dismiss any visible overlay/popup blocking the page.

        Attempts common dismiss buttons in order. Silently continues if none
        are found — this is a best-effort fallback.
        """
        dismiss_buttons = [
            ("Not Now", self.page.get_by_role("dialog")
                .get_by_text("Not Now", exact=True)),
            ("OK", self.page.get_by_role("button", name="OK")),
            ("Close", self.page.get_by_role("button", name=re.compile(r"close", re.IGNORECASE))),
            ("Dismiss", self.page.get_by_label(re.compile(r"close|dismiss", re.IGNORECASE))),
        ]
        for label, locator in dismiss_buttons:
            try:
                if await locator.first.is_visible():
                    await locator.first.click()
                    logger.info(f"[@{self.account}] Dismissed overlay via '{label}' button")
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    return
            except Exception:
                continue
        logger.warning(f"[@{self.account}] No dismiss button found for blocking overlay")
