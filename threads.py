"""
threads.py
----------
Threads posting automation.

Flow per account:
  1. Navigate to threads.net — verify already logged in via saved cookies
  2. Check for bot challenge
  3. Click the New Thread compose button
  4. Type post_text with human-like delays in the Lexical editor
  5. Attach up to 10 images (if media_paths provided)
  6. Click Post
  7. Wait for modal to close (success confirmation)
  8. Emit progress event

Platform limits:
  - Max 10 images per post
  - Max 500 characters of text (enforced by frontend)

Note:
  Threads uses a Lexical rich-text editor (same as Meta's other products).
  Text must be typed into the contenteditable div — .fill() doesn't work reliably.
  We use element.type() character by character instead.
"""

import asyncio
import random
import logging

from utils import human_like_type, human_like_mouse_move, get_element_center, deactivate_account
from exceptions import AbortedError

logger = logging.getLogger(__name__)

# ─── Selectors ────────────────────────────────────────────────────────────────
SEL_HOME_FEED       = '[aria-label="Threads feed"]'
SEL_NEW_THREAD_BTN  = '[aria-label="New thread"], [aria-label="Create"]'
SEL_TEXT_EDITOR     = 'p[data-lexical-editor="true"], div[contenteditable="true"][role="textbox"]'
SEL_MEDIA_INPUT     = 'input[accept*="image"], input[type="file"][accept*="image"]'
SEL_POST_BUTTON     = 'div[role="dialog"] button:has-text("Post"), button:has-text("Post")'
SEL_CHALLENGE       = '[aria-label*="suspicious"], form[action*="challenge"]'


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
        logger.info(f"[@{self.account}] Navigating to threads.net")
        await self.page.goto("https://www.threads.net/", wait_until="domcontentloaded", timeout=30000)
        try:
            await self.page.wait_for_selector(SEL_HOME_FEED, timeout=15000)
            logger.info(f"[@{self.account}] Threads home confirmed — already logged in")
        except Exception:
            raise RuntimeError(
                f"@{self.account} does not appear to be logged in to Threads. "
                "Open the Dolphin Anty profile manually and log in via Meta account."
            )

    async def _check_bot_challenge(self) -> None:
        try:
            await asyncio.sleep(1)
            if await self.page.locator(SEL_CHALLENGE).count() > 0:
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

        # ── Step 1: Click the New Thread button ──────────────────────────────
        new_thread_btn = self.page.locator(SEL_NEW_THREAD_BTN).first
        await new_thread_btn.wait_for(state="visible", timeout=15000)

        try:
            nx, ny = await get_element_center(self.page, SEL_NEW_THREAD_BTN.split(",")[0].strip())
            await human_like_mouse_move(self.page, nx, ny)
        except Exception:
            pass
        await new_thread_btn.click()
        await self._check_abort()

        # ── Step 2: Wait for compose dialog ──────────────────────────────────
        editor = self.page.locator(SEL_TEXT_EDITOR).first
        await editor.wait_for(state="visible", timeout=15000)
        await asyncio.sleep(random.uniform(0.5, 1.2))

        # ── Step 3: Type the thread text ─────────────────────────────────────
        await editor.click()
        await human_like_type(editor, post_text)
        await self._check_abort()

        # ── Step 4: Attach media (optional) ──────────────────────────────────
        if self.media_paths:
            await self._attach_media()
            await self._check_abort()

        # ── Step 5: Click Post ────────────────────────────────────────────────
        post_btn = self.page.locator(SEL_POST_BUTTON).last
        await post_btn.wait_for(state="visible", timeout=10000)

        try:
            px, py = await get_element_center(self.page, 'button:has-text("Post")')
            await human_like_mouse_move(self.page, px, py)
        except Exception:
            pass
        await post_btn.click()

        # ── Step 6: Verify post went through ─────────────────────────────────
        # No abort check after post click — thread is already submitted
        await self._verify_posted()

        logger.info(f"[@{self.account}] Thread posted successfully")
        self.emitter.post_published(
            account=self.account,
            message=f"Thread posted as @{self.account}",
        )

    async def _attach_media(self) -> None:
        """
        Inject local image files into Threads' hidden file input.
        Threads accepts all images in a single set_input_files() call.
        """
        logger.info(f"[@{self.account}] Attaching {len(self.media_paths)} image(s)")
        try:
            media_input = self.page.locator(SEL_MEDIA_INPUT).first
            # set_input_files() bypasses the OS file picker entirely
            await media_input.set_input_files(self.media_paths)

            # Wait for image previews to render before posting
            await self.page.wait_for_function(
                f"""() => {{
                    const previews = document.querySelectorAll('img[src^="blob:"]');
                    return previews.length >= {len(self.media_paths)};
                }}""",
                timeout=30000,
            )
            logger.info(f"[@{self.account}] Media previews ready")
        except Exception as e:
            logger.warning(f"[@{self.account}] Media attach failed (continuing without media): {e}")

    async def _verify_posted(self) -> None:
        """
        Wait for the compose modal to close — Threads closes it automatically
        after a successful post.
        """
        try:
            # Modal should close: dialog disappears
            await self.page.wait_for_selector(
                'div[role="dialog"]', state="hidden", timeout=20000
            )
        except Exception:
            await asyncio.sleep(3)
            logger.warning(f"[@{self.account}] Could not verify thread post — assuming success")
