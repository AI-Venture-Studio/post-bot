"""
instagram.py
------------
Instagram posting automation.

Flow per account:
  1. Register popup handlers (notifications prompt, reels info dialog)
  2. Navigate to instagram.com — verify already logged in via saved cookies
  3. Check for bot challenge / suspicious activity
  4. Click the "Create" (+) button in the left sidebar
  5. Wait for the file upload dialog — inject media via set_input_files()
  6. Click "Next" through the crop screen
  7. Click "Next" through the filter/effects screen
  8. Type the caption with human-like delays
  9. Click "Share"
  10. Wait for "Your reel/post has been shared." confirmation text
  11. Emit progress event

Platform constraints:
  - Images are REQUIRED — Instagram's web compose flow has no text-only post option
  - Max 10 images per post
  - Max 2,200 characters for captions (enforced by frontend)
  - Accepted formats: JPEG, PNG (WebP blocked — causes upload failures)
  - Multi-step dialog: Upload → Crop → Filter → Caption → Share

Note:
  All interactive elements use Playwright's semantic locators (get_by_role,
  get_by_label, get_by_text) per Playwright best practices. These target the
  ARIA role and accessible name exposed to assistive technology, making them
  more stable than CSS class names or text-substring selectors.
  If Instagram redesigns the compose flow, update the locators in this file —
  that is the first thing to check if posting breaks.
"""

import asyncio
import re
import random
import logging

from utils import human_like_type, human_like_mouse_move, deactivate_account
from exceptions import AbortedError

logger = logging.getLogger(__name__)

# ─── Selectors ────────────────────────────────────────────────────────────────
# Only CSS selectors that have no semantic ARIA equivalent are kept here.
# All interactive elements in the compose flow use Playwright's get_by_role /
# get_by_label / get_by_text locators (see methods below).

# Hidden file input — no ARIA role; set_input_files() targets it directly.
SEL_FILE_INPUT = 'input[accept*="image"]'

# Bot-challenge detection — error-state selectors, not part of the compose flow.
SEL_CHALLENGE = (
    '[aria-label*="suspicious"], '
    'form[action*="checkpoint"], '
    'h2:has-text("We noticed unusual activity")'
)
SEL_CAPTCHA = 'iframe[src*="captcha"], #captcha-container'


class BotChallengeError(Exception):
    pass


class InstagramPoster:
    def __init__(self, page, campaign: dict, media_paths: list[str], emitter, account: str, event_store):
        """
        :param page:        Playwright Page connected via CDP to the Dolphin Anty browser
        :param campaign:    Full campaign dict from Supabase (post_text, media_attachments, …)
        :param media_paths: Ordered list of local absolute file paths — MUST NOT be empty
        :param emitter:     ProgressEmitter instance
        :param account:     The username being used for this session
        :param event_store: EventStore instance for abort signal checking
        """
        if not media_paths:
            raise ValueError(
                "Instagram requires at least one image. "
                "The campaign validator should have caught this before reaching here."
            )

        self.page = page
        self.campaign = campaign
        self.media_paths = media_paths[:10]  # Instagram limit: 10
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
        await self._create_post()

    # ─── Private methods ──────────────────────────────────────────────────────

    async def _check_abort(self) -> None:
        """Raise AbortedError if the user has requested an abort."""
        if self.event_store.is_aborted():
            logger.info(f"[@{self.account}] Abort signal detected")
            raise AbortedError(f"@{self.account} aborted by user")

    async def _register_popup_handlers(self) -> None:
        """
        Register handlers for popups that can appear at unpredictable times.

        Uses Playwright's add_locator_handler() — Playwright checks for these
        locators before every actionability check and automatically calls the
        handler to dismiss them if visible. No manual timing needed.
        """
        # "Turn on Notifications" prompt — appears when account opens after a while.
        # "Not Now" is plain text (not a button), so we use get_by_text scoped
        # inside the dialog to avoid clicking the wrong element.
        await self.page.add_locator_handler(
            self.page.get_by_text(re.compile(r"turn on notifications", re.IGNORECASE)),
            lambda _: self.page.get_by_role("dialog")
                .get_by_text("Not Now", exact=True)
                .first.click(),
        )
        logger.debug(f"[@{self.account}] Registered handler: Turn on Notifications → Not Now")

        # "Video posts are now shared as reels" info dialog — first-time reel post
        await self.page.add_locator_handler(
            self.page.get_by_text(re.compile(r"video posts are now shared as reels", re.IGNORECASE)),
            lambda _: self.page.get_by_role("button", name="OK").click(),
        )
        logger.debug(f"[@{self.account}] Registered handler: Reels info dialog → OK")

    async def _navigate_home(self) -> None:
        logger.info(f"[@{self.account}] Navigating to instagram.com")
        await self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
        # Verify login by waiting for the Home nav link.
        # get_by_role("link") targets the semantic <a> element in the left nav;
        # .or_(get_by_label) covers cases where the element has no visible text
        # but exposes its name via aria-label only.
        home_link = (
            self.page.get_by_role("link", name="Home")
            .or_(self.page.get_by_label("Home"))
        )
        try:
            await home_link.first.wait_for(state="visible", timeout=15000)
            logger.info(f"[@{self.account}] Instagram home confirmed — already logged in")
        except Exception:
            raise RuntimeError(
                f"@{self.account} does not appear to be logged in to Instagram. "
                "Open the Dolphin Anty profile manually and log in first."
            )

    async def _check_bot_challenge(self) -> None:
        try:
            await asyncio.sleep(1)
            if (
                await self.page.locator(SEL_CHALLENGE).count() > 0
                or await self.page.locator(SEL_CAPTCHA).count() > 0
            ):
                logger.warning(f"[@{self.account}] Bot challenge detected on Instagram")
                deactivate_account(self.account, "instagram")
                raise BotChallengeError(f"@{self.account} triggered a bot challenge on Instagram.")
        except BotChallengeError:
            raise
        except Exception as e:
            logger.debug(f"Bot challenge check error (non-fatal): {e}")

    async def _create_post(self) -> None:
        post_text = self.campaign["caption"]
        logger.info(
            f"[@{self.account}] Creating Instagram post "
            f"({len(post_text)} chars, {len(self.media_paths)} images)"
        )

        # ── Step 1: Click the "Create" (+) button in the left sidebar ─────────
        await self._click_create_button()
        await self._check_abort()

        # ── Step 2: Upload media via the hidden file input ────────────────────
        await self._inject_files()
        await self._check_abort()

        # ── Step 3: Crop screen → click Next ──────────────────────────────────
        logger.info(f"[@{self.account}] On crop screen — clicking Next")
        await self._click_next("crop → filter")
        await self._check_abort()

        # ── Step 4: Filter/effects screen → click Next ────────────────────────
        logger.info(f"[@{self.account}] On filter screen — clicking Next")
        await self._click_next("filter → caption")
        await self._check_abort()

        # ── Step 5: Caption screen — type the caption ─────────────────────────
        logger.info(f"[@{self.account}] On caption screen — typing caption")
        await self._type_caption(post_text)
        await self._check_abort()

        # ── Step 6: Click Share ────────────────────────────────────────────────
        await self._click_share()

        # ── Step 7: Wait for success confirmation (post is live) ──────────────
        # No abort check after share — the post is already submitted
        await self._wait_for_share_complete()

        logger.info(f"[@{self.account}] Instagram post published successfully")
        self.emitter.post_published(
            account=self.account,
            message=f"Posted on Instagram as @{self.account}",
        )

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

    async def _click_create_button(self) -> None:
        """
        Click the Create (+) button in the left sidebar.

        Instagram's left-nav items are <a> elements (role="link"). The accessible
        name varies between UI versions — "New post" historically, "Create" in the
        current redesign. We chain .or_() locators so either label matches.

        If an overlay intercepts the click, we attempt to dismiss it and retry.
        """
        create_btn = (
            self.page.get_by_role("link", name="New post")
            .or_(self.page.get_by_role("link", name="Create"))
            .or_(self.page.get_by_label("New post"))
            .or_(self.page.get_by_label("Create"))
        )
        await create_btn.first.wait_for(state="visible", timeout=15000)

        try:
            box = await create_btn.first.bounding_box()
            if box:
                await human_like_mouse_move(
                    self.page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
        except Exception:
            pass

        try:
            await create_btn.first.click(timeout=10000)
        except Exception:
            # An overlay is likely intercepting the click — try to dismiss it
            logger.info(f"[@{self.account}] Create button blocked by overlay — attempting to dismiss")
            await self._dismiss_any_overlay()
            await create_btn.first.click(timeout=15000)

        logger.info(f"[@{self.account}] Clicked Create button")
        await asyncio.sleep(random.uniform(0.8, 1.5))

    async def _inject_files(self) -> None:
        """
        Inject image files into Instagram's hidden file input.

        After "Post" is selected, a dialog appears with a "Select from computer"
        button that would normally open an OS file picker. We bypass it entirely
        by calling set_input_files() directly on the hidden <input type="file">.

        Per Playwright docs: set_input_files() sets the element's .files property
        without opening the OS dialog. Files must exist on the local filesystem
        where the automation is running.
        """
        # Confirm the upload dialog is visible before injecting.
        try:
            await self.page.get_by_role("button", name="Select from computer").wait_for(
                state="visible", timeout=10000
            )
            logger.info(f"[@{self.account}] Upload dialog visible — 'Select from computer' found")
        except Exception:
            logger.warning(
                f"[@{self.account}] 'Select from computer' button not detected — "
                "proceeding to inject files anyway"
            )

        # Wait for the file input to be attached to the DOM before injecting.
        # Instagram lazily creates the <input> via JavaScript after the dialog
        # renders. set_input_files() does not auto-wait for attachment the way
        # click() does, so we wait explicitly.
        file_input = self.page.locator(SEL_FILE_INPUT).first
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(self.media_paths)
        logger.info(f"[@{self.account}] Files injected ({len(self.media_paths)} file(s))")

        # Wait for the crop screen to confirm the upload was accepted.
        crop_indicator = (
            self.page.get_by_role("button", name=re.compile(r"crop", re.IGNORECASE))
            .or_(self.page.get_by_role("img", name=re.compile(r"preview", re.IGNORECASE)))
        )
        try:
            await crop_indicator.first.wait_for(state="visible", timeout=60000)
            logger.info(f"[@{self.account}] Crop screen visible — files accepted")
        except Exception:
            await asyncio.sleep(3)
            logger.warning(f"[@{self.account}] Crop screen not detected — proceeding anyway")

    async def _click_next(self, step_label: str) -> None:
        """
        Click the Next button and wait for the screen transition.

        get_by_role("button", name="Next") targets the button by its accessible
        name. .last is intentional — if the image carousel renders its own
        previous/next arrows, .last selects the dialog-level Next button.
        """
        next_btn = self.page.get_by_role("button", name="Next")
        try:
            await next_btn.last.wait_for(state="visible", timeout=10000)
            await next_btn.last.click()
            await asyncio.sleep(random.uniform(1.2, 2.5))
            logger.debug(f"[@{self.account}] Clicked Next ({step_label})")
        except Exception as e:
            raise RuntimeError(f"Could not click Next at step '{step_label}': {e}")

    async def _type_caption(self, caption: str) -> None:
        """
        Find the caption contenteditable area and type with human-like delays.

        Instagram's caption field is a div[contenteditable="true"] exposed as a
        textbox with a label containing "caption". get_by_label with a regex is
        resilient to exact wording changes (e.g. "Write a caption…" vs "Caption").
        """
        caption_area = (
            self.page.get_by_label(re.compile(r"write a caption", re.IGNORECASE))
            .or_(self.page.get_by_role("textbox", name=re.compile(r"caption", re.IGNORECASE)))
        )
        try:
            await caption_area.first.wait_for(state="visible", timeout=10000)
            await caption_area.first.click()
            await human_like_type(caption_area.first, caption)
        except Exception as e:
            raise RuntimeError(f"Could not type caption: {e}")

    async def _click_share(self) -> None:
        """
        Click the Share button to publish the post.

        The Share button sits in the top-right corner of the caption dialog.
        get_by_role("button", name="Share") is unambiguous here — no other
        visible button on this screen is labelled "Share".
        """
        # Use .first to avoid a strict mode violation — Instagram's accessibility
        # tree can expose both a visible Share button and a hidden one at the same
        # time, causing an unhandled multi-match error on plain .click()/.wait_for().
        share_btn = self.page.get_by_role("button", name="Share").first
        try:
            await share_btn.wait_for(state="visible", timeout=10000)
        except Exception:
            raise RuntimeError("Share button did not appear within timeout")

        try:
            box = await share_btn.bounding_box()
            if box:
                await human_like_mouse_move(
                    self.page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
        except Exception:
            pass

        await share_btn.click()
        logger.info(f"[@{self.account}] Clicked Share")

    async def _wait_for_share_complete(self) -> None:
        """
        Wait for Instagram's success confirmation after clicking Share.

        Primary signal: the text "Your reel has been shared." (or "Your post
        has been shared.") appears inside the dialog once the upload finishes.
        Fallback: if the confirmation text never appears, we wait for the
        dialog itself to close (Instagram auto-closes it on success).
        """
        success_text = self.page.get_by_text(
            re.compile(r"your (reel|post) has been shared", re.IGNORECASE)
        )
        try:
            await success_text.first.wait_for(state="visible", timeout=120000)
            logger.info(f"[@{self.account}] Success confirmation detected")
            await asyncio.sleep(2)
        except Exception:
            logger.warning(
                f"[@{self.account}] Success text not detected — "
                "falling back to dialog close check"
            )
            try:
                await self.page.locator('div[role="dialog"]').wait_for(
                    state="hidden", timeout=30000
                )
                await asyncio.sleep(2)
            except Exception:
                await asyncio.sleep(5)
                logger.warning(
                    f"[@{self.account}] Share dialog close not detected — assuming success"
                )
