"""
app.py
------
Post-Bot Flask backend.

Responsibilities:
  - Serve API endpoints for progress polling, abort, and manual campaign start
  - Manage the full lifecycle: pre-flight → browser launch → automation → cleanup
  - Store real-time progress events in an in-memory EventStore

Architecture note:
  Campaigns are started manually via /api/start, which spawns a background thread
  with its own asyncio event loop. Flask itself is synchronous (single-threaded
  for simplicity). Progress is stored in a global EventStore object, which the
  Flask routes read on each poll request.
"""

import asyncio
import os
import threading
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import create_client, Client

from dolphin import DolphinAntyClient
from instagram import InstagramPoster, BotChallengeError as IGChallengeError
from twitter import TwitterPoster, BotChallengeError as XChallengeError
from threads import ThreadsPoster, BotChallengeError as ThreadsChallengeError
import media_manager
import utils
from logger import setup_logging

load_dotenv()
setup_logging()

from exceptions import AbortedError

# ─────────────────────────────────────────────────────────────────────────────
# Supabase client
# ─────────────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # service role for storage access
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

media_manager.init_media_manager(supabase)
utils.init_utils(supabase)

# ─────────────────────────────────────────────────────────────────────────────
# EventStore — in-memory progress state
# ─────────────────────────────────────────────────────────────────────────────

class EventStore:
    """
    Holds real-time progress for the currently running campaign.
    Not persisted — cleared when a new campaign starts.
    Accessible from both the background runner thread and Flask request threads.
    """
    def __init__(self):
        self.checkpoints: list[dict] = []
        self.current_progress: int = 0
        self.status: str = "idle"        # idle | running | completed | error
        self.abort_signal: bool = False
        self.post_count: int = 0
        self.latest_sentence: str = "Waiting to start..."
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.checkpoints = []
            self.current_progress = 0
            self.status = "idle"
            self.abort_signal = False
            self.post_count = 0
            self.latest_sentence = "Waiting to start..."

    def is_aborted(self) -> bool:
        with self.lock:
            return self.abort_signal

    def set_abort(self):
        with self.lock:
            self.abort_signal = True

    def set_status(self, status: str):
        with self.lock:
            self.status = status

    def set_progress(self, progress: int):
        with self.lock:
            self.current_progress = min(100, max(0, progress))

    def add_checkpoint(self, event_type: str, status: str, message: str,
                       target: str = None, index: int = None, total: int = None):
        with self.lock:
            checkpoint = {
                "type": event_type,
                "status": status,
                "message": message,
                "target": target,
                "index": index,
                "total": total,
                "timestamp": datetime.now().isoformat(),
            }
            self.checkpoints.append(checkpoint)
            self.latest_sentence = message

            if event_type == "post" and status == "success":
                self.post_count += 1

            status_icon = "✓" if status == "success" else "✗"
            print(f"[CHECKPOINT] {status_icon} [{event_type.upper()}] {message}")

            return checkpoint

    def get_current_state(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "progress": self.current_progress,
                "latest_sentence": self.latest_sentence,
                "total_events": len(self.checkpoints),
                "post_count": self.post_count,
            }


event_store = EventStore()


# ─────────────────────────────────────────────────────────────────────────────
# ProgressEmitter — semantic wrapper over EventStore
# ─────────────────────────────────────────────────────────────────────────────

class ProgressEmitter:
    """
    Provides named checkpoint methods so automation classes don't touch EventStore directly.
    Only final outcomes are emitted (no retry noise).
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── Campaign checkpoints ──────────────────────────────────────────────────

    def campaign_starting(self, campaign_id: str, platform: str, accounts: list[str]):
        event_store.set_status("running")
        event_store.set_progress(0)
        n = len(accounts)
        event_store.add_checkpoint(
            event_type="campaign",
            status="success",
            message=f"Campaign started — {platform.capitalize()}, {n} account{'s' if n != 1 else ''}",
        )

    def campaign_completed(self, message: str = "Campaign completed"):
        event_store.set_progress(100)
        event_store.set_status("completed")
        event_store.add_checkpoint(
            event_type="campaign",
            status="success",
            message=message,
        )

    def campaign_failed(self, reason: str = None):
        event_store.set_progress(100)
        event_store.set_status("error")
        message = f"Campaign failed: {reason}" if reason else "Campaign failed"
        event_store.add_checkpoint(
            event_type="campaign",
            status="failure",
            message=message,
        )

    def campaign_aborted(self):
        event_store.set_status("aborted")
        event_store.add_checkpoint(
            event_type="campaign",
            status="failure",
            message="Campaign aborted by user",
        )

    # ── Account checkpoints ───────────────────────────────────────────────────

    def account_starting(self, account: str, platform: str):
        event_store.add_checkpoint(
            event_type="account",
            status="success",
            message=f"Starting session for @{account} ({platform})",
            target=account,
        )

    # ── Post checkpoints ─────────────────────────────────────────────────────

    def post_published(self, account: str, message: str = None,
                       index: int = None, total: int = None):
        event_store.add_checkpoint(
            event_type="post",
            status="success",
            message=message or f"Posted as @{account}",
            target=account,
            index=index,
            total=total,
        )

    def post_failed(self, account: str, reason: str = None,
                    index: int = None, total: int = None):
        message = f"Post failed (@{account})"
        if reason:
            message += f": {reason}"
        event_store.add_checkpoint(
            event_type="post",
            status="failure",
            message=message,
            target=account,
            index=index,
            total=total,
        )


emitter = ProgressEmitter()


# ─────────────────────────────────────────────────────────────────────────────
# Campaign runner helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_campaign_by_id(campaign_id: str) -> dict | None:
    """Fetch a campaign by its campaign_id."""
    result = (
        supabase.table("post_campaigns")
        .select("*")
        .eq("campaign_id", campaign_id)
        .limit(1)
        .execute()
    )
    rows = result.data
    return rows[0] if rows else None


def update_campaign_status(campaign_id: str, status: str):
    supabase.table("post_campaigns") \
        .update({"status": status, "updated_at": datetime.now().isoformat()}) \
        .eq("campaign_id", campaign_id) \
        .execute()


def get_account_record(username: str, platform: str) -> dict | None:
    result = (
        supabase.table("social_accounts")
        .select("*")
        .eq("username", username)
        .eq("platform", platform)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    rows = result.data
    return rows[0] if rows else None


def update_account_last_used(username: str, platform: str):
    supabase.table("social_accounts") \
        .update({"last_used_at": datetime.now().isoformat()}) \
        .eq("username", username) \
        .eq("platform", platform) \
        .execute()


def preflight_check(campaign: dict) -> tuple[bool, str]:
    """
    Validate all preconditions before starting a campaign.
    Returns (ok: bool, reason: str).

    Checks:
      1. user_accounts, caption present and non-empty
      2. Each account exists in social_accounts with is_active=True
      3. Each account has a browser_profile set
      4. Dolphin Anty is reachable
      5. media_urls (if any) all exist in Supabase Storage
      6. Instagram campaigns have at least one media attachment
    """
    platform = campaign.get("platform")
    user_accounts = campaign.get("user_accounts") or []
    caption = (campaign.get("caption") or "").strip()
    media_urls = campaign.get("media_urls") or []

    if not user_accounts:
        return False, "Campaign has no user_accounts"
    if not caption:
        return False, "Campaign has no caption"
    if platform == "instagram" and not media_urls:
        return False, "Instagram campaigns require at least one media attachment"

    for username in user_accounts:
        record = get_account_record(username, platform)
        if not record:
            return False, f"Account @{username} not found or is inactive for platform {platform}"
        if not record.get("browser_profile"):
            return False, f"Account @{username} has no browser_profile configured"

    preflight_dolphin = DolphinAntyClient()
    if not preflight_dolphin.login(show_progress=True):
        return False, "Dolphin Anty is not reachable at the configured local API URL"

    if media_urls:
        all_exist, missing = media_manager.verify_media_exists_in_storage(media_urls)
        if not all_exist:
            return False, f"Media files missing from storage: {missing}"

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Per-account automation runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_account(
    account: str,
    campaign: dict,
    media_paths: list[str],
) -> None:
    """
    Launch a Dolphin Anty browser session for `account` and run the platform poster.
    Handles: profile lookup → start → CDP connect → automation → close.
    """
    platform = campaign["platform"]
    record = get_account_record(account, platform)
    browser_profile_name = record["browser_profile"]

    # ── Instantiate Dolphin per-account ────────────────────────────────────────
    dolphin = DolphinAntyClient()
    profile_id = None
    playwright_instance = None
    browser = None

    try:
        # ── Login to Dolphin Anty ──────────────────────────────────────────────
        if not dolphin.login(show_progress=True):
            raise RuntimeError("Failed to connect to Dolphin Anty")

        # ── Find Dolphin profile ───────────────────────────────────────────────
        print(f"[>>] Looking for assigned profile: {browser_profile_name}")

        profile = dolphin.find_profile_by_name(browser_profile_name)
        if not profile:
            profile = dolphin.find_profile_by_id(browser_profile_name)

        if not profile:
            raise RuntimeError(f"Browser profile '{browser_profile_name}' not found in Dolphin Anty")

        profile_id = profile.get('id')
        print(f"[OK] Found profile: {profile.get('name')} (ID: {profile_id})")
        print(f"\n[>>] Starting profile: {profile.get('name')} (ID: {profile_id})")

        # ── Start browser session ──────────────────────────────────────────────
        emitter.account_starting(account, platform)
        automation_info = dolphin.start_profile(profile_id, headless=False)
        if not automation_info:
            raise RuntimeError("Failed to start Dolphin Anty profile")

        # ── Build CDP URL ──────────────────────────────────────────────────────
        ws_endpoint = automation_info.get('wsEndpoint')
        port = automation_info.get('port')

        from urllib.parse import urlparse
        dolphin_url = os.getenv('DOLPHIN_LOCAL_API_URL', 'http://localhost:3001')
        parsed_url = urlparse(dolphin_url)
        dolphin_host = parsed_url.hostname or 'localhost'

        if ws_endpoint.startswith('/'):
            cdp_url = f"ws://{dolphin_host}:{port}{ws_endpoint}"
        elif ws_endpoint.startswith('ws://') or ws_endpoint.startswith('wss://'):
            cdp_url = ws_endpoint
        else:
            cdp_url = f"ws://{dolphin_host}:{port}/{ws_endpoint}"

        print(f'[OK] Profile started!')
        print(f'   WebSocket Path: {ws_endpoint}')
        print(f'   Port: {port}')
        print(f'   Full CDP URL: {cdp_url}')

        # ── Connect Playwright ─────────────────────────────────────────────────
        print(f'\n🔗 Connecting Playwright to Dolphin Anty browser...')
        from playwright.async_api import async_playwright
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.connect_over_cdp(cdp_url)
        print('[OK] Playwright connected to Dolphin Anty browser!\n')

        # ── Create a fresh tab for automation ────────────────────────────────
        # Existing tabs may be on other sites (e.g. x.com) — always open a
        # new tab so we start clean. It is closed during cleanup.
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
        else:
            context = await browser.new_context()
        page = await context.new_page()

        # ── Run platform automation ────────────────────────────────────────────
        if platform == "instagram":
            poster = InstagramPoster(page, campaign, media_paths, emitter, account, event_store)
        elif platform == "x":
            poster = TwitterPoster(page, campaign, media_paths, emitter, account, event_store)
        elif platform == "threads":
            poster = ThreadsPoster(page, campaign, media_paths, emitter, account, event_store)
        else:
            raise ValueError(f"Unsupported platform: {platform}")

        await poster.run()
        update_account_last_used(account, platform)

    except AbortedError:
        print(f'[ABORT] @{account} aborted by user')
        raise

    except (IGChallengeError, XChallengeError, ThreadsChallengeError) as e:
        emitter.post_failed(account, reason=str(e))
        raise

    except Exception as e:
        emitter.post_failed(account, reason=f"Error posting as @{account}: {e}")
        raise

    finally:
        print(f'\n[CLEANUP] Cleaning up browser resources for @{account}...')
        try:
            if page:
                await page.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
            if playwright_instance:
                await playwright_instance.stop()
        except Exception as e:
            print(f'[WARN] Error during cleanup: {e}')

        try:
            if profile_id:
                dolphin.stop_profile(profile_id)
                print(f'[OK] Browser profile stopped')
        except Exception as e:
            print(f'[WARN] Could not stop profile: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Campaign runner
# ─────────────────────────────────────────────────────────────────────────────

async def process_campaign(campaign: dict) -> None:
    """
    Run a single campaign end to end:
      download media → for each account: run_account() → cleanup media
    """
    campaign_id = campaign["campaign_id"]
    platform = campaign["platform"]
    user_accounts = campaign["user_accounts"]
    post_delay = campaign.get("post_delay", 15)
    media_urls = campaign.get("media_urls") or []

    # Compute total steps for progress bar (one step per account)
    total = len(user_accounts)

    event_store.clear()

    print('\n' + '='*70)
    print(f'[CAMPAIGN] {campaign_id}')
    print('='*70)

    emitter.campaign_starting(campaign_id, platform, user_accounts)

    print(f'\n[STATUS] Updating campaign status to: in-progress')
    update_campaign_status(campaign_id, "in-progress")
    print(f'[DB] Updated campaign {campaign_id} status to: in-progress')

    print(f'\n[INFO] Campaign will run using {total} account(s):')
    for idx, acc in enumerate(user_accounts, 1):
        print(f'    {idx}. @{acc}')

    # ── Download media files locally ──────────────────────────────────────────
    media_paths: list[str] = []
    if media_urls:
        try:
            media_paths = media_manager.download_campaign_media(campaign_id, media_urls)
        except Exception as e:
            update_campaign_status(campaign_id, "failed")
            emitter.campaign_failed(reason=f"Media download failed: {e}")
            return

    # ── Run each account ──────────────────────────────────────────────────────
    failed = False
    for i, account in enumerate(user_accounts):
        if event_store.is_aborted():
            print('\n[ABORT] Abort signal detected — stopping campaign')
            update_campaign_status(campaign_id, "aborted")
            print(f'[DB] Updated campaign {campaign_id} status to: aborted')
            emitter.campaign_aborted()
            break

        print('\n' + '='*70)
        print(f'[ACCOUNT] {i + 1}/{total}: @{account}')
        print('='*70)
        print(f'[INFO] Attempting to use account @{account}...')

        try:
            await run_account(account, campaign, media_paths)
        except Exception as e:
            print(f'\n[ERR] Account @{account} failed: {e}')
            failed = True
            # Continue to next account rather than stopping the whole campaign

        # Progress update
        event_store.set_progress(int(((i + 1) / total) * 95))  # 95% max until final cleanup

        # Check for abort before proceeding to next account
        if event_store.is_aborted():
            print(f'\n[ABORT] Abort signal detected - skipping remaining accounts')
            break

        # Delay between accounts (skip after the last one)
        if i < total - 1 and not event_store.is_aborted():
            print(f'\n[WAIT] Waiting {post_delay}s before starting next account...\n')
            await asyncio.sleep(post_delay)

    # ── Final status ──────────────────────────────────────────────────────────
    if event_store.is_aborted():
        print(f'\n[STATUS] Updating campaign status to: aborted')
        update_campaign_status(campaign_id, "aborted")
        print(f'[DB] Updated campaign {campaign_id} status to: aborted')
        emitter.campaign_aborted()
    else:
        state = event_store.get_current_state()
        final_status = "failed" if failed and state["post_count"] == 0 else "completed"
        print(f'\n[STATUS] Updating campaign status to: {final_status}')
        update_campaign_status(campaign_id, final_status)
        print(f'[DB] Updated campaign {campaign_id} status to: {final_status}')
        msg = f"Campaign {final_status} — {state['post_count']}/{total} posts published"
        if final_status == "completed":
            print(f'\n[OK] {msg}')
            emitter.campaign_completed(msg)
        else:
            print(f'\n[ERR] {msg}')
            emitter.campaign_failed(reason=msg)

    # ── Cleanup media ─────────────────────────────────────────────────────────
    media_manager.delete_local_campaign_dir(campaign_id)
    # Do NOT delete media from Supabase Storage here.
    # Files must persist for retry (failed/aborted) and re-run (completed) flows.


def run_campaign_in_thread(campaign_id: str) -> None:
    """Run a campaign in a background thread with its own asyncio event loop."""
    try:
        campaign = get_campaign_by_id(campaign_id)
        if not campaign:
            print(f'[ERR] Campaign {campaign_id} not found')
            event_store.set_status("error")
            return

        print(f'[DB] Found campaign {campaign_id} (status: {campaign.get("status", "unknown")})')

        print('\n[CHECK] Running pre-flight validation checks...')
        ok, reason = preflight_check(campaign)
        if not ok:
            print(f'[WARN] Campaign {campaign_id} failed pre-flight: {reason}')
            update_campaign_status(campaign_id, "failed")
            emitter.campaign_failed(reason=f"Pre-flight failed: {reason}")
            return

        print(f'[OK] All pre-flight checks passed!')

        print(f'\n[INFO] Processing campaign: {campaign_id}')
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(process_campaign(campaign))

    except Exception as e:
        print(f'[ERR] Campaign runner error: {e}')
        event_store.set_status("error")


# ─────────────────────────────────────────────────────────────────────────────
# Flask app
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.json.compact = False
CORS(app, origins=os.environ.get("ALLOWED_ORIGINS", "*"))


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "message": "Social Media Post Bot API by AIVS",
        "status": "running",
        "version": "1.0 - Instagram Integration",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "bot": "post-bot"})


@app.route("/api/start", methods=["POST"])
def start():
    """Start a specific campaign by campaign_id."""
    if event_store.status == "running":
        return jsonify({"error": "Automation already running"}), 400

    data = request.get_json(silent=True) or {}
    campaign_id = data.get("campaign_id")

    if not campaign_id:
        return jsonify({"error": "campaign_id is required"}), 400

    campaign = get_campaign_by_id(campaign_id)
    if not campaign:
        return jsonify({"error": "Campaign not found"}), 404

    if campaign["status"] != "not-started":
        return jsonify({"error": f"Campaign cannot be started (status: {campaign['status']})"}), 409

    event_store.clear()
    event_store.set_status("running")

    thread = threading.Thread(
        target=run_campaign_in_thread,
        args=(campaign_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "message": f"Campaign {campaign_id} started"})


@app.route("/api/abort", methods=["POST"])
def abort():
    event_store.set_abort()
    print('[ABORT] Abort signal set')
    return jsonify({"message": "Abort signal sent"})


@app.route("/api/progress/current", methods=["GET"])
def progress_current():
    return jsonify(event_store.get_current_state())


@app.route("/api/progress/checkpoints", methods=["GET"])
def progress_checkpoints():
    state = event_store.get_current_state()
    with event_store.lock:
        checkpoints = list(reversed(event_store.checkpoints.copy()))
    return jsonify({
        "checkpoints": checkpoints,
        "total": state["total_events"],
        "post_count": state["post_count"],
        "status": state["status"],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Clean up any leftover media files from previous interrupted campaigns
    media_manager.cleanup_orphan_temp_files()

    port = int(os.environ.get("PORT", 8001))
    production = os.environ.get("PRODUCTION", "false").lower() == "true"

    print('[SERVER] Starting Post Bot API Server...')
    print(f'[API] Health: http://localhost:{port}/health')
    print(f'[API] Start Campaign: POST http://localhost:{port}/api/start')
    print(f'[API] Current Progress: http://localhost:{port}/api/progress/current')
    print(f'[ENV] Production mode: {production}')
    print('[INFO] Manual-start mode — send POST /api/start to begin automation')

    if production:
        from waitress import serve
        print(f'[SERVER] Running with Waitress on 0.0.0.0:{port}')
        serve(app, host="0.0.0.0", port=port)
    else:
        print(f'[SERVER] Development mode - debug enabled')
        app.run(host="0.0.0.0", port=port, debug=False)
