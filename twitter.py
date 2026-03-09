"""
twitter.py
----------
X/Twitter posting automation.

Flow per account:
  1. Navigate to x.com/home — verify already logged in via saved cookies
  2. Check for bot challenge / suspicious activity prompts
  3. Click the compose area
  4. Type post_text with human-like delays
  5. Attach up to 4 images (if media_paths provided)
  6. Click the Post button
  7. Verify the compose box cleared (post went through)
  8. Emit progress event

Platform limits:
  - Max 4 images per tweet
  - Max 280 characters of text (enforced by frontend; not re-validated here)
"""

import asyncio
import random
import logging

from utils import human_like_type, human_like_mouse_move, get_element_center, deactivate_account
from exceptions import AbortedError

logger = logging.getLogger(__name__)

# ─── Selectors ────────────────────────────────────────────────────────────────
# These target the 2025 X web interface.  If X changes its DOM, update here.
SEL_HOME_FEED       = '[data-testid="primaryColumn"]'
SEL_COMPOSE_BOX     = '[data-testid="tweetTextarea_0"]'
SEL_FILE_INPUT      = 'input[data-testid="fileInput"]'
SEL_POST_BUTTON     = '[data-testid="tweetButtonInline"], [data-testid="tweetButton"]'
SEL_CHALLENGE       = 'input[name="challenge_response"], [data-testid="ocfEnterTextNextButton"]'
SEL_LOCKED          = '[data-testid="AccountSuspendedBody"]'


class BotChallengeError(Exception):
    pass


class TwitterPoster:
    def __init__(self, page, campaign: dict, media_paths: list[str], emitter, account: str, event_store):
        """
        :param page:        Playwright Page connected via CDP to the Dolphin Anty browser
        :param campaign:    Full campaign dict from Supabase (post_text, post_delay, …)
        :param media_paths: Ordered list of local absolute file paths to attach (may be empty)
        :param emitter:     ProgressEmitter instance
        :param account:     The username being used for this session
        :param event_store: EventStore instance for abort signal checking
        """
        self.page = page
        self.campaign = campaign
        self.media_paths = media_paths[:4]  # X hard limit: 4 images
        self.emitter = emitter
        self.account = account
        self.event_store = event_store

    async def run(self) -> None:
        """Entry point called by app.py for each account."""
        await self._navigate_home()
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
        logger.info(f"[@{self.account}] Navigating to x.com/home")
        await self.page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        try:
            await self.page.wait_for_selector(SEL_HOME_FEED, timeout=15000)
            logger.info(f"[@{self.account}] Home feed confirmed — already logged in")
        except Exception:
            raise RuntimeError(
                f"@{self.account} does not appear to be logged in. "
                "Open the Dolphin Anty profile manually and log in first."
            )

    async def _check_bot_challenge(self) -> None:
        """Detect Twitter bot challenges or suspended account screens."""
        try:
            await asyncio.sleep(1)
            # Challenge prompt (verify it's you)
            if await self.page.locator(SEL_CHALLENGE).count() > 0:
                logger.warning(f"[@{self.account}] Bot challenge detected on X")
                deactivate_account(self.account, "x")
                raise BotChallengeError(f"@{self.account} triggered a bot challenge on X.")

            # Suspended account
            if await self.page.locator(SEL_LOCKED).count() > 0:
                logger.warning(f"[@{self.account}] Account suspended on X")
                deactivate_account(self.account, "x")
                raise BotChallengeError(f"@{self.account} is suspended on X.")

        except BotChallengeError:
            raise
        except Exception as e:
            logger.debug(f"Bot challenge check error (non-fatal): {e}")

    async def _compose_and_post(self) -> None:
        post_text = self.campaign["caption"]
        logger.info(f"[@{self.account}] Composing tweet ({len(post_text)} chars, {len(self.media_paths)} images)")

        # ── Step 1: Click the compose box ────────────────────────────────────
        compose = self.page.locator(SEL_COMPOSE_BOX).first
        await compose.wait_for(state="visible", timeout=15000)

        try:
            cx, cy = await get_element_center(self.page, SEL_COMPOSE_BOX)
            await human_like_mouse_move(self.page, cx, cy)
        except Exception:
            pass
        await compose.click()
        await self._check_abort()

        # ── Step 2: Type the tweet text ───────────────────────────────────────
        await human_like_type(compose, post_text)
        await self._check_abort()

        # ── Step 3: Attach media (optional) ──────────────────────────────────
        if self.media_paths:
            await self._attach_media()
            await self._check_abort()

        # ── Step 4: Click Post ────────────────────────────────────────────────
        post_btn = self.page.locator(SEL_POST_BUTTON).first
        await post_btn.wait_for(state="visible", timeout=10000)

        try:
            bx, by = await get_element_center(self.page, SEL_POST_BUTTON.split(",")[0].strip())
            await human_like_mouse_move(self.page, bx, by)
        except Exception:
            pass
        await post_btn.click()

        # ── Step 5: Verify tweet was posted ──────────────────────────────────
        # No abort check after post click — tweet is already submitted
        await self._verify_posted()

        logger.info(f"[@{self.account}] Tweet posted successfully")
        self.emitter.post_published(
            account=self.account,
            message=f"Tweeted as @{self.account}",
        )

    async def _attach_media(self) -> None:
        """Inject local image files into X's hidden file input."""
        logger.info(f"[@{self.account}] Attaching {len(self.media_paths)} image(s)")
        try:
            file_input = self.page.locator(SEL_FILE_INPUT).first
            await file_input.set_input_files(self.media_paths)

            # Wait for upload progress to finish
            await self.page.wait_for_function(
                """() => {
                    const uploading = document.querySelectorAll('[aria-label*="Loading"]');
                    return uploading.length === 0;
                }""",
                timeout=60000,
            )
            logger.info(f"[@{self.account}] Media uploaded")
        except Exception as e:
            logger.warning(f"[@{self.account}] Media attach failed (continuing without media): {e}")

    async def _verify_posted(self) -> None:
        """
        Wait for the compose box to clear, indicating the tweet was submitted.
        Falls back to a simple sleep if the selector check fails.
        """
        try:
            selector = SEL_COMPOSE_BOX.replace("'", "\\'")
            await self.page.wait_for_function(
                f"""() => {{
                    const box = document.querySelector('{selector}');
                    return !box || box.textContent.trim() === '';
                }}""",
                timeout=15000,
            )
        except Exception:
            # If verification fails, give it a moment and move on
            await asyncio.sleep(3)
            logger.warning(f"[@{self.account}] Could not verify tweet — assuming success")
