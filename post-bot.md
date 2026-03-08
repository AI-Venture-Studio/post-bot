# Social Media Post Bot — Full System Documentation

> A complete guide to understanding, replicating, and extending the post-bot backend.
> This bot uses the same architecture as the comment-bot — read this alongside comment-bot.md.

---

## Table of Contents

1. [What Is This Project?](#1-what-is-this-project)
2. [How It Differs from the Comment-Bot](#2-how-it-differs-from-the-comment-bot)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Directory Structure](#4-directory-structure)
5. [Prerequisites & Setup](#5-prerequisites--setup)
   - [Dolphin Anty Setup](#51-dolphin-anty-setup)
   - [Supabase Setup](#52-supabase-setup)
   - [Backend Setup](#53-backend-setup)
6. [Database Schema](#6-database-schema)
7. [How a Campaign Works — End to End](#7-how-a-campaign-works--end-to-end)
8. [Dolphin Anty — Session Management](#8-dolphin-anty--session-management)
9. [Human-Like Behavior Simulation](#9-human-like-behavior-simulation)
10. [Platform-Specific Automation](#10-platform-specific-automation)
    - [Instagram](#101-instagram)
    - [X/Twitter](#102-xtwitter)
    - [Threads](#103-threads)
11. [Media Handling](#11-media-handling)
12. [Real-Time Progress System](#12-real-time-progress-system)
13. [API Endpoints Reference](#13-api-endpoints-reference)
14. [Environment Variables Reference](#14-environment-variables-reference)
15. [Common Errors & Fixes](#15-common-errors--fixes)
16. [Required Client Integration](#16-required-client-integration)

---

## 1. What Is This Project?

This is a **multi-platform social media post automation system**. It allows you to:

- Configure campaigns that post content to your own social accounts on Instagram, X (Twitter), or Threads
- Have one or more accounts automatically publish a post (text + optional images)
- Monitor campaign progress in real time via a web dashboard
- Queue multiple campaigns and run them sequentially

It is architecturally identical to the comment-bot. The only difference is what the automation does after connecting to the browser: instead of navigating to a target's profile and commenting, it navigates to the platform's compose flow and creates a new post.

---

## 2. How It Differs from the Comment-Bot

| Aspect | Comment-Bot | Post-Bot |
|--------|-------------|----------|
| Goal | Comment on target profiles' posts | Post to own account's profile |
| `target_profiles` | Yes — array of usernames to visit | No — not needed |
| `targeting_mode` | Yes — `date` or `posts` count | No — not needed |
| Comment/post text field | `custom_comment` | `post_text` |
| Platform automation | Navigate to target → collect posts → comment on each | Navigate to compose → fill text → attach media → post |
| Post delay | Between comments on different posts | Between accounts publishing the same content |
| Instagram media | Optional (Instagram doesn't support image comments) | **Required** — Instagram web can only create media posts |
| DB table | `comment_campaigns` | `post_campaigns` |
| Flask port | `5001` | `5002` |

**Everything else is identical:** Dolphin Anty session flow, EventStore, ProgressEmitter, media download/cleanup, queue runner, abort mechanism, pre-flight validation, human-like behavior utilities.

---

## 3. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  BROWSER (User / Operator)                  │
│                                                             │
│   Next.js Frontend (React)                                  │
│   ├─ /post-bot/configure  → Create post campaigns          │
│   └─ /post-bot/queue      → Manage & monitor campaigns     │
└───────────────────────┬─────────────────────────────────────┘
                        │ HTTP/JSON
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                  Flask Backend (Python)  port 5002          │
│   ├─ app.py          → Routes, queue runner, EventStore     │
│   ├─ instagram.py    → Instagram Playwright automation      │
│   ├─ twitter.py      → X/Twitter Playwright automation      │
│   ├─ threads.py      → Threads Playwright automation        │
│   ├─ dolphin.py      → DolphinAntyClient                   │
│   ├─ media_manager.py → Image download, verify, cleanup    │
│   └─ utils.py        → human_like_type, mouse_move, etc.   │
└──────────┬────────────────────────┬────────────────────────┘
           │                        │
           ▼                        ▼
┌─────────────────┐      ┌───────────────────────┐
│   Supabase      │      │   Dolphin Anty        │
│                 │      │   (Anti-Detect Browser)│
│  PostgreSQL DB  │      │                       │
│  ├─ post_       │      │  Each social account  │
│  │  campaigns   │      │  has its own browser  │
│  └─ social_     │      │  profile with unique  │
│     accounts    │      │  fingerprint, cookies │
│                 │      │  and proxies          │
│  Storage Bucket │      └──────────┬────────────┘
│  └─ campaign-   │                 │
│     media       │                 │ Chrome DevTools Protocol (CDP)
└─────────────────┘                 ▼
                          ┌─────────────────────┐
                          │   Playwright        │
                          │   Browser Instance  │
                          │   (Chromium)        │
                          └────────┬────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              Instagram           X           Threads
              (Compose,        (Compose      (Compose
              upload images,   tweet + media, thread + media,
              add caption,     post)          post)
              share)
```

**Data flow summary:**
1. You create a campaign in the frontend (Next.js).
2. Campaign saved to Supabase. If images attached, they upload to Supabase Storage first.
3. The Flask backend detects the new campaign via webhook or polling.
4. Flask starts the Dolphin Anty browser profile for the assigned social account.
5. Playwright connects to that browser via CDP.
6. The automation opens the platform, navigates to compose, types the post, attaches media, and publishes.
7. Progress events are stored in memory and polled by the frontend every 2 seconds.
8. After completion, media files are cleaned up.

---

## 4. Directory Structure

```
post-bot-server/
│
├── app.py                  # Flask routes, EventStore, ProgressEmitter, queue runner
├── instagram.py            # Instagram automation class (InstagramPoster)
├── twitter.py              # X/Twitter automation class (TwitterPoster)
├── threads.py              # Threads automation class (ThreadsPoster)
├── dolphin.py              # DolphinAntyClient — browser session management
├── media_manager.py        # Image download/upload/cleanup
├── utils.py                # Shared: human_like_type, mouse_move, deactivate_account
├── requirements.txt
├── .env.example
└── bot-media/              # Created at runtime; git-ignored
    └── {campaign_id}/
        └── image.jpg       # Temp files downloaded from Supabase Storage
```

---

## 5. Prerequisites & Setup

### 5.1 Dolphin Anty Setup

Dolphin Anty must be **open and running** whenever the Flask backend is active. Its local REST API (at `http://localhost:3001`) is only available while the desktop app is open.

**You only need to do this once per social account:**

1. Open the Dolphin Anty desktop app
2. Create a browser profile for each social account (name it descriptively, e.g. `instagram_acct1`)
3. Assign a proxy to each profile (strongly recommended — use a proxy matching the account's country)
4. Start the profile, manually log in to the social platform, complete any 2FA
5. Close the profile — cookies are now saved

**Every subsequent automation run:**
- The profile already has saved cookies → no manual login needed
- The backend automatically stops and starts the profile via the API

**Get your API token:**
1. In Dolphin Anty desktop: **Settings → API**
2. Copy the token → add to `.env` as `DOLPHIN_API_TOKEN`

---

### 5.2 Supabase Setup

**Step 1: Create the `post_campaigns` table**

Run this in the Supabase SQL Editor:

```sql
CREATE TABLE post_campaigns (
  id                UUID      PRIMARY KEY DEFAULT uuid_generate_v4(),
  campaign_id       TEXT      UNIQUE NOT NULL,
  platform          TEXT      NOT NULL,
  post_text         TEXT      NOT NULL,
  user_accounts     TEXT[]    NOT NULL,
  post_delay        INTEGER   DEFAULT 15,
  status            TEXT      DEFAULT 'not-started',
  queue_position    INTEGER,
  media_attachments JSONB[],
  created_at        TIMESTAMP DEFAULT NOW(),
  updated_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_post_campaigns_status ON post_campaigns(status);
CREATE INDEX idx_post_campaigns_queue  ON post_campaigns(queue_position);
```

**Step 2: Verify `social_accounts` table exists**

The post-bot reads from the same `social_accounts` table used by the comment-bot. If you've already set that up, nothing else is needed. If not:

```sql
CREATE TABLE social_accounts (
  id              UUID      PRIMARY KEY DEFAULT uuid_generate_v4(),
  username        TEXT      NOT NULL,
  password        TEXT,
  platform        TEXT      NOT NULL,
  browser_profile TEXT      NOT NULL,
  is_active       BOOLEAN   DEFAULT true,
  last_used_at    TIMESTAMP,
  created_at      TIMESTAMP DEFAULT NOW(),
  updated_at      TIMESTAMP DEFAULT NOW(),
  UNIQUE(username, platform)
);
```

**Step 3: Storage bucket**

Use the existing `campaign-media` bucket (shared with comment-bot). If it doesn't exist:
1. Supabase → Storage → Create bucket
2. Name: `campaign-media`
3. Set to **Private** (backend uses service role key to access it)

---

### 5.3 Backend Setup

```bash
cd post-bot-server

# Create virtual environment
python -m venv venv
source venv/bin/activate        # Mac/Linux
# OR
venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Create environment file
cp .env.example .env
```

Edit `.env` with your values (see Section 14 for full reference).

```bash
# Run development server
python app.py
# Backend starts at http://localhost:5002
```

---

## 6. Database Schema

### `post_campaigns`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key (auto-generated) |
| `campaign_id` | TEXT | Human-readable ID (e.g. `post_campaign_abc123_1702475982`) |
| `platform` | TEXT | `instagram`, `x`, or `threads` |
| `post_text` | TEXT | The text content to post |
| `user_accounts` | TEXT[] | Array of account usernames that will post |
| `post_delay` | INTEGER | Seconds to wait between accounts posting (default 15) |
| `status` | TEXT | `not-started`, `in-progress`, `completed`, `failed`, `aborted` |
| `queue_position` | INTEGER | Determines processing order |
| `media_attachments` | JSONB[] | Array of `{storage_path, file_name}` objects |
| `created_at` | TIMESTAMP | Auto-set on creation |
| `updated_at` | TIMESTAMP | Auto-set on update |

### `social_accounts` (shared with comment-bot)

| Column | Type | Description |
|--------|------|-------------|
| `username` | TEXT | Account username |
| `platform` | TEXT | `instagram`, `x`, or `threads` |
| `browser_profile` | TEXT | **Exact name** of the Dolphin Anty profile (case-insensitive match) |
| `is_active` | BOOLEAN | If false, account is skipped (set after bot challenge detection) |
| `last_used_at` | TIMESTAMP | Updated after each campaign run |

### Campaign Status Lifecycle

```
not-started  →  in-progress  →  completed
                             ├→  failed
                             └→  aborted
```

---

## 7. How a Campaign Works — End to End

### Step 1: Campaign Created (Frontend)

The frontend inserts a row into `post_campaigns` with `status = 'not-started'`. If media files are attached, they are uploaded to Supabase Storage first (see Section 11). A webhook is then fired to the backend.

### Step 2: Backend Detects the Campaign

A background polling loop runs every ~10 seconds. It queries:
```sql
SELECT * FROM post_campaigns
WHERE status = 'not-started'
ORDER BY queue_position ASC
LIMIT 1
```

### Step 3: Pre-Flight Validation

Before doing anything browser-related:

| Check | What fails |
|-------|-----------|
| `user_accounts` is non-empty | Campaign has no accounts |
| `post_text` is non-empty | Campaign has no content |
| Instagram + no media | Instagram requires images |
| Each account in `social_accounts` with `is_active = true` | Account not found or deactivated |
| Each account has `browser_profile` set | Profile name not configured |
| Dolphin Anty reachable at `DOLPHIN_LOCAL_API_URL` | Desktop app not open |
| All `media_attachments` exist in Supabase Storage | Files deleted or upload incomplete |

If any check fails, the campaign stays `not-started` and is retried on the next poll cycle.

### Step 4: Status → `in-progress`

The campaign row is updated in Supabase.

### Step 5: Media Download

If `media_attachments` is non-empty, all files are downloaded from Supabase Storage to `bot-media/{campaign_id}/` on the local filesystem. The local paths are passed to the automation classes.

### Step 6: For Each Account — Browser Session

For each username in `user_accounts`:

1. Look up the account in `social_accounts` → get `browser_profile` name
2. Call `DolphinAntyClient.find_profile_by_name(name)` → get `profile_id`
3. Call `DolphinAntyClient.start_and_wait(profile_id)` → get `automationPort`
4. Connect Playwright:
   ```python
   browser = await chromium.connect_over_cdp(f"ws://localhost:{port}")
   page = await browser.new_page()
   ```
5. Instantiate the platform poster and call `await poster.run()`
6. Update `last_used_at` for the account
7. Close the browser, stop the Dolphin profile
8. Wait `post_delay` seconds before the next account

### Step 7: Final Status

- All accounts succeeded → `completed`
- Some accounts failed but at least one posted → `completed` (partial success)
- All accounts failed → `failed`
- Abort was requested → `aborted`

### Step 8: Media Cleanup

- Delete `bot-media/{campaign_id}/` from local filesystem
- Delete all files from Supabase Storage

---

## 8. Dolphin Anty — Session Management

### Why It's Needed

Social platforms detect bots through browser fingerprints (canvas, WebGL, fonts, timezone), behavioral patterns, and IP reputation. Dolphin Anty creates isolated browser profiles, each with a unique realistic fingerprint. Paired with proxies and human-like behavior, each session appears to be a different human user.

### Profile Startup Sequence (from `dolphin.py`)

```
1. FIND profile by name
   └── GET http://localhost:3001/v1.0/browser_profiles?limit=200
       Iterate response, match profile['name'].lower() == name.lower()
       Extract profile_id

2. STOP any running instance
   └── GET http://localhost:3001/v1.0/browser_profiles/{profile_id}/stop
       (errors are ignored — the profile may already be stopped)

3. START the profile
   └── GET http://localhost:3001/v1.0/browser_profiles/{profile_id}/start?automation=1
       Returns: { automation: { port: <int>, wsEndpoint: <str> } }

4. WAIT 10 seconds (grace period for Chromium process startup)

5. POLL the automationPort for TCP connectivity
   └── Try socket.create_connection("localhost", port) every 0.75 seconds
   └── Timeout after 90 seconds total
   └── Log "still waiting…" every 10 seconds

6. VERIFY Chrome DevTools Protocol endpoint
   └── GET http://localhost:{port}/json/version
   └── Expect 200 OK
   └── Timeout after 20 seconds

7. RETURN the port number

ON FAILURE:
   └── Retry up to 3 times (8 second cooldown between retries)
   └── HTTP 401/403/404 → permanent failure (no retry)
```

### Profile Naming

The `browser_profile` field in `social_accounts` stores the **display name** of the profile exactly as it appears in Dolphin Anty. The backend matches this case-insensitively. If the name doesn't match, the campaign fails pre-flight with "Profile not found".

---

## 9. Human-Like Behavior Simulation

All interactions mimic human behavior to avoid platform detection. These utilities live in `utils.py`.

### Typing (`human_like_type`)

| Event | Delay |
|-------|-------|
| Pre-typing hesitation | 800–2000 ms |
| Between regular characters | 220–320 ms |
| After completing a word (space) | 400–1200 ms |
| After punctuation (. ! ?) | 800–2500 ms |
| Post-typing review pause | 2500–6000 ms |
| Typo + backspace | 7% chance per character |

### Mouse Movement (`human_like_mouse_move`)

Before every significant click:
- Mouse moves along a quadratic Bezier curve from current position to target
- 30 micro-steps with ease-in-out speed profile (slow at start and end)
- 30% chance of slight overshoot followed by a correction micro-move
- 100–400 ms pause before the click executes

### Post Compose Flow (per account)

```
1. Navigate to platform home (verify login)
2. Check for bot challenge
3. Open compose (click button or compose area)
4. Pre-typing hesitation
5. Type post text character by character
6. Post-typing review pause
7. Attach media files (if any) via set_input_files()
8. Human mouse move to Post/Share button
9. Click Post/Share
10. Verify post went through
11. Wait post_delay seconds before next account
```

---

## 10. Platform-Specific Automation

### 10.1 Instagram

**File:** `instagram.py` — `InstagramPoster`

**Important constraint:** Instagram's web interface can only create posts with at least one image. There is no text-only post option in the standard compose flow. The frontend enforces this, and the backend validates it during pre-flight.

**Compose flow:**

```
Navigate to https://www.instagram.com/
  ↓
Verify home feed ([aria-label="Home"] visible)
  ↓
Check bot challenge ([aria-label*="suspicious"] or captcha iframe)
  ↓
Click [aria-label="New post"] in left nav
  ↓
Dialog opens with "Select from computer" button
  ↓
set_input_files() on input[accept*="image"] (up to 10 files)
  ↓
Wait for crop screen to appear (button[aria-label*="crop"])
  ↓
Click Next → (crop screen)
  ↓
Click Next → (filter screen)
  ↓
Caption textarea appears (div[aria-label*="Write a caption"])
  ↓
human_like_type() the caption
  ↓
Click div[role="button"]:has-text("Share")
  ↓
Wait for dialog to close (div[role="dialog"] state=hidden)
  ↓
emit_post_result(success=True)
```

**Key selectors:**
- New post button: `[aria-label="New post"]`
- File input: `input[accept*="image"]`
- Next button: `button:has-text("Next")` (clicked twice)
- Caption area: `div[aria-label*="Write a caption"]`
- Share button: `div[role="button"]:has-text("Share")`

---

### 10.2 X/Twitter

**File:** `twitter.py` — `TwitterPoster`

**Images:** Optional, max 4. Multiple images are set in sequence using `set_input_files()`.

**Compose flow:**

```
Navigate to https://x.com/home
  ↓
Verify home feed ([data-testid="primaryColumn"] visible)
  ↓
Check for challenge/suspension
  ↓
Click [data-testid="tweetTextarea_0"] (compose box)
  ↓
human_like_type() the tweet text
  ↓
If media: set_input_files() on input[data-testid="fileInput"]
  Wait for loading indicators to disappear
  ↓
Human mouse move to Post button
  ↓
Click [data-testid="tweetButtonInline"] or [data-testid="tweetButton"]
  ↓
Wait for compose box to clear (tweet submitted)
  ↓
emit_post_result(success=True)
```

---

### 10.3 Threads

**File:** `threads.py` — `ThreadsPoster`

**Images:** Optional, max 10. Threads accepts all files in a single `set_input_files()` call.

**Compose flow:**

```
Navigate to https://www.threads.net/
  ↓
Verify home feed ([aria-label="Threads feed"] visible)
  ↓
Check for bot challenge
  ↓
Click [aria-label="New thread"] or [aria-label="Create"]
  ↓
Compose modal opens
  ↓
Click into p[data-lexical-editor="true"] (Lexical rich-text editor)
  ↓
human_like_type() the thread text
  (Note: .fill() doesn't work on Lexical editors — must use .type() per character)
  ↓
If media: set_input_files() on input[accept*="image"]
  Wait for blob: URL image previews to appear
  ↓
Click button:has-text("Post")
  ↓
Wait for modal to close (div[role="dialog"] state=hidden)
  ↓
emit_post_result(success=True)
```

**Lexical editor note:** Threads uses Meta's Lexical framework for its text editor. Standard Playwright `.fill()` does not work because Lexical intercepts input at the framework level. Characters must be typed individually using `.type()`, which dispatches real keyboard events that Lexical processes correctly.

---

## 11. Media Handling

### Flow Overview

```
Frontend                     Supabase Storage              Backend (Flask)
─────────                    ────────────────              ──────────────
File selected by user
  ↓
campaign_id generated
  ↓
Upload file:
  campaign-media/
  {campaign_id}/
  {file_name}
  ↓
Insert post_campaigns row
  with media_attachments:
  [{storage_path, file_name}]
                                                           Pre-flight:
                                                           verify_media_exists_in_storage()
                                                             ↓
                                                           download_campaign_media()
                                                           → bot-media/{campaign_id}/{file_name}
                                                             ↓
                                                           Pass local paths to poster
                                                             ↓
                                                           poster._attach_media()
                                                           → set_input_files(local_paths)
                                                             ↓
                                                           Cleanup:
                                                           delete_local_campaign_dir()
                                                           delete_campaign_media_from_storage()
```

### Why `set_input_files()` Works

Normally, `<input type="file">` opens an OS file picker that automation tools cannot control. Playwright's `set_input_files()` bypasses this by directly setting the element's `.files` property to local file paths, making the browser treat them as if the user picked them via the OS dialog. This is why files must be downloaded to the local machine where Flask is running — the browser needs actual local file paths.

### Storage Path Format

```
campaign-media/
└── post_campaign_abc12345_1702475982/
    ├── photo1.jpg
    └── photo2.png
```

### Cleanup Tiers

**Tier 1: Local temp files**
```python
delete_local_campaign_dir(campaign_id)
# Deletes: bot-media/post_campaign_abc12345_1702475982/
```

**Tier 2: Supabase Storage**
```python
delete_campaign_media_from_storage(media_attachments)
# Removes: campaign-media/post_campaign_abc12345_1702475982/*
```

**Tier 3: Startup orphan cleanup**
```python
# On server start: delete bot-media/ subdirs older than 24 hours
cleanup_orphan_temp_files()
```

---

## 12. Real-Time Progress System

### Architecture

The backend keeps an in-memory `EventStore` (not persisted to Supabase):

```python
class EventStore:
    checkpoints: List[Dict]    # Final outcome events shown in UI
    current_progress: int      # 0–100
    status: str                # idle | running | completed | error
    abort_signal: bool         # Set by /api/abort
    post_count: int            # Successful posts in this campaign
    latest_sentence: str       # Most recent status message
```

A `ProgressEmitter` singleton wraps it with named methods:

```python
emitter.emit_campaign_start(campaign_id, platform, accounts)
emitter.emit_account_start(account, platform)
emitter.emit_post_result(account, success, message)
emitter.emit_campaign_end(success, message)
```

Only **final outcomes** become checkpoints — no retries, no intermediate noise.

### Frontend Polling

Every 2 seconds:
```
GET /api/progress/current       → { status, progress, post_count, latest_sentence }
GET /api/progress/checkpoints   → { checkpoints, total, post_count, status }
```

### Checkpoint Event Shape

```json
{
  "type": "post",
  "status": "success",
  "message": "Tweeted as @account1",
  "account": "account1",
  "timestamp": "2026-03-05T10:01:30"
}
```

Types: `campaign` | `account` | `post`
Statuses: `success` | `failure` | `info`

### Abort Flow

```
Frontend: POST /api/abort
  ↓
event_store.abort_signal = True
  ↓
campaign_queue_loop() checks is_aborted() at top of each account loop
  ↓
If True: update status → 'aborted', emit campaign_end, break loop
```

---

## 13. API Endpoints Reference

All endpoints on the Flask backend (default port 5002).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Returns `{ status: "healthy", bot: "post-bot" }` |
| POST | `/api/start` | No-op acknowledgment (queue runs continuously) |
| POST | `/api/abort` | Send abort signal to running campaign |
| GET | `/api/progress/current` | Current status + progress % |
| GET | `/api/progress/checkpoints` | Checkpoint events for UI carousel |
| POST | `/api/webhook/campaign-added` | Called when new campaign is created |

### Example: `/api/progress/checkpoints` Response

```json
{
  "checkpoints": [
    {
      "type": "campaign",
      "status": "success",
      "message": "Campaign started — Instagram, 3 accounts",
      "campaign_id": "post_campaign_abc123_1702475982",
      "timestamp": "2026-03-05T10:00:00"
    },
    {
      "type": "account",
      "status": "info",
      "message": "Starting session for @account1 (instagram)",
      "account": "account1",
      "timestamp": "2026-03-05T10:00:02"
    },
    {
      "type": "post",
      "status": "success",
      "message": "Posted on Instagram as @account1",
      "account": "account1",
      "timestamp": "2026-03-05T10:01:45"
    },
    {
      "type": "post",
      "status": "failure",
      "message": "Error posting as @account2: CDP connection timeout",
      "account": "account2",
      "timestamp": "2026-03-05T10:03:00"
    }
  ],
  "total": 4,
  "post_count": 1,
  "status": "running"
}
```

---

## 14. Environment Variables Reference

### Backend (`.env` in `post-bot-server/`)

| Variable | Required | Description |
|----------|----------|-------------|
| `DOLPHIN_API_TOKEN` | Yes | API token from Dolphin Anty Settings → API |
| `DOLPHIN_LOCAL_API_URL` | Yes | Default: `http://localhost:3001` |
| `SUPABASE_URL` | Yes | Your Supabase project URL |
| `SUPABASE_ANON_KEY` | Yes | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Service role key (required for Storage access) |
| `PORT` | No | Server port (default: `5002`) |
| `PRODUCTION` | No | `true` on Windows VPS (uses Waitress instead of Gunicorn) |
| `ALLOWED_ORIGINS` | No | CORS origins (default: `*`) |

**Why `5002` and not `5001`?** The comment-bot backend runs on 5001. Using 5002 allows both backends to run simultaneously on the same machine without conflict.

---

## 15. Common Errors & Fixes

### "Profile not found" on campaign start
**Cause:** The `browser_profile` name in `social_accounts` doesn't match any Dolphin Anty profile.
**Fix:** Open Dolphin Anty, find the exact profile name, update the `browser_profile` field in the database. The match is case-insensitive but spelling must be exact.

### "CDP port did not open within 90 seconds"
**Cause:** Dolphin Anty's browser didn't start in time, or a stale instance is blocking the port.
**Fix:** Open Dolphin Anty, manually stop any running profiles. Check Task Manager / Activity Monitor for zombie `chrome` processes and kill them.

### "Dolphin Anty is not reachable"
**Cause:** The Dolphin Anty desktop app is not open.
**Fix:** Open the Dolphin Anty application. The local API at `http://localhost:3001` only runs while the app is open.

### Campaign stays `not-started` forever
**Cause:** Pre-flight validation is failing.
**Fix:** Check Flask logs for the specific failure message — common causes are inactive accounts, missing browser_profile, or missing media files.

### "Instagram requires at least one image"
**Cause:** An Instagram campaign was created without media attachments.
**Fix:** The frontend should prevent this. If it reached the backend, the campaign will stay `not-started` indefinitely. Delete it and recreate with images attached.

### Instagram dialog doesn't open / compose flow breaks
**Cause:** Instagram updated their web UI and the selectors changed.
**Fix:** Open the Dolphin Anty profile manually, inspect the compose dialog in DevTools, and update the selectors in `instagram.py`. The aria-labels are usually stable but Instagram occasionally renames them.

### Threads text not typed correctly
**Cause:** Threads uses a Lexical rich-text editor. Using `.fill()` instead of character-by-character `.type()` bypasses Lexical's event listeners and the text doesn't register.
**Fix:** Ensure `human_like_type()` in `utils.py` uses `.type()` (dispatches keyboard events), not `.fill()`.

### Images not attaching
**Cause:** Download failed, local paths are wrong, or the file input selector changed.
**Fix:** Check that `bot-media/{campaign_id}/` was created and contains the files. Check Supabase Storage to confirm the files exist. Check Flask logs for download errors.

### Bot challenge detected / account deactivated
**Cause:** The platform flagged the account.
**Fix:** The account is automatically set to `is_active = false`. Manually re-enable it in `social_accounts` after resolving the challenge inside the Dolphin Anty profile browser. Consider assigning a better proxy.

---

## 16. Required Client Integration

> This section documents all changes the frontend (`client/`) needs to integrate with the post-bot backend. **No client files are modified by this backend.** This is documentation only.

---

### 16.1 New Environment Variable

Add to `client/.env.local`:
```
NEXT_PUBLIC_POST_BOT_API_URL=http://localhost:5002
```

### 16.2 New npm Packages

```bash
npm install @supabase/supabase-js @dnd-kit/core @dnd-kit/sortable @dnd-kit/utilities nanoid
```

The current `client/package.json` does not include these. They are needed for Supabase CRUD, queue drag-and-drop reordering, and campaign ID generation respectively.

### 16.3 New Pages

Create these three pages:

**`client/app/post-bot/page.tsx`** — Replace the existing stub
- Hub page with links to `/post-bot/configure` and `/post-bot/queue`
- Include `<ServerStatus />` component (shows Flask health)
- Uses existing `<Breadcrumb />` component

**`client/app/post-bot/configure/page.tsx`** — Campaign creation
- Renders the `<ConfigurePost />` component
- Protected route (requires login)

**`client/app/post-bot/queue/page.tsx`** — Queue + live progress
- Renders `<CampaignQueueTable />` and `<CampaignProgress />`
- Polls `/api/progress/current` and `/api/progress/checkpoints` every 2 seconds

### 16.4 New Components

Create in `client/components/post-bot/`:

**`configure-post.tsx`** — Main campaign creation form

State needed:
```typescript
platform: "instagram" | "x" | "threads"
selectedAccounts: string[]      // from social_accounts filtered by platform
postText: string
postDelay: number               // default 15
mediaFiles: File[]
mediaPreviews: string[]         // URL.createObjectURL() for thumbnails
isSubmitting: boolean
```

Form sections:
1. Platform selector (Instagram | X | Threads)
2. Account multi-select (load from `social_accounts` filtered by platform)
3. Post text textarea with character counter:
   - Instagram: 2,200 char limit
   - X: 280 char limit
   - Threads: 500 char limit
4. Media upload zone with platform constraints:

```typescript
const MEDIA_CONSTRAINTS = {
  instagram: { maxFiles: 10, maxSizeMB: 8,  required: true,  accept: "image/jpeg,image/png,image/webp" },
  x:         { maxFiles: 4,  maxSizeMB: 5,  required: false, accept: "image/jpeg,image/png,image/gif,image/webp" },
  threads:   { maxFiles: 10, maxSizeMB: 8,  required: false, accept: "image/jpeg,image/png,image/webp" },
};
```

5. Post delay input (seconds, range 8–60)

`handleSubmit` flow:
```
1. Validate (Instagram: must have ≥1 image)
2. campaignId = `post_campaign_${nanoid(8)}_${Date.now()}`
3. Upload each file to Supabase Storage:
   Path: campaign-media/{campaignId}/{file.name}
4. Build mediaAttachments: [{ storage_path, file_name }]
5. Insert into post_campaigns (status: 'not-started', queue_position: next)
6. POST /api/webhook/campaign-added to port 5002
7. Redirect to /post-bot/queue
```

**`campaign-progress.tsx`** — Real-time progress display

Polls every 2 seconds:
- `GET {POST_BOT_API_URL}/api/progress/current` → progress bar + status badge
- `GET {POST_BOT_API_URL}/api/progress/checkpoints` → checkpoint carousel

Renders:
- Status badge: idle / running / completed / error
- Progress bar (0–100%)
- Auto-scrolling checkpoint list (newest at bottom)
  - `campaign` type: blue
  - `account` type: gray
  - `post` success: green / failure: red
- Post count: "X posts published"
- Abort button → `POST {POST_BOT_API_URL}/api/abort`

**`campaign-queue-table.tsx`** — Campaign queue table

Reads from `post_campaigns` ordered by `queue_position`. Columns:
- Drag handle (reorder)
- Campaign ID (truncated)
- Platform (icon from `lucide-react`)
- Accounts (count)
- Post text (truncated, 60 chars)
- Status badge
- Created at
- Delete button (only for `not-started` campaigns)

On drop: batch update `queue_position` values in Supabase.

**`server-status.tsx`** — Backend health indicator

Polls `GET {POST_BOT_API_URL}/health` every 30 seconds.
- Green dot + "Server online" if 200
- Red dot + "Server offline" if unreachable

### 16.5 New Lib Files

Create in `client/lib/post-bot/`:

**`types/campaign.ts`**
```typescript
export type PostCampaignStatus = "not-started" | "in-progress" | "completed" | "failed" | "aborted";
export type PostCampaignPlatform = "instagram" | "x" | "threads";

export interface MediaAttachment {
  storage_path: string;
  file_name: string;
}

export interface PostCampaign {
  id: string;
  campaign_id: string;
  platform: PostCampaignPlatform;
  post_text: string;
  user_accounts: string[];
  post_delay: number;
  status: PostCampaignStatus;
  queue_position: number | null;
  media_attachments: MediaAttachment[] | null;
  created_at: string;
  updated_at: string;
}

export interface ProgressCheckpoint {
  type: "campaign" | "account" | "post";
  status: "success" | "failure" | "info";
  message: string;
  account?: string;
  campaign_id?: string;
  timestamp: string;
}

export interface ProgressState {
  status: "idle" | "running" | "completed" | "error";
  progress: number;
  post_count: number;
  latest_sentence: string;
}
```

**`api-client.ts`**
```typescript
const BASE = process.env.NEXT_PUBLIC_POST_BOT_API_URL;

export const postBotApi = {
  health:          () => fetch(`${BASE}/health`).then(r => r.json()),
  getProgress:     () => fetch(`${BASE}/api/progress/current`).then(r => r.json()),
  getCheckpoints:  () => fetch(`${BASE}/api/progress/checkpoints`).then(r => r.json()),
  abort:           () => fetch(`${BASE}/api/abort`, { method: "POST" }).then(r => r.json()),
  triggerWebhook:  () => fetch(`${BASE}/api/webhook/campaign-added`, { method: "POST" }).then(r => r.json()),
};
```

**`supabase-client.ts`**
```typescript
// Assumes a shared Supabase client is available at @/lib/supabase
// (you'll need to create this if it doesn't exist yet)

export const postCampaignsClient = {
  getAll:                () => supabase.from("post_campaigns").select("*").order("queue_position"),
  create:                (data) => supabase.from("post_campaigns").insert(data).select().single(),
  delete:                (campaignId) => supabase.from("post_campaigns").delete().eq("campaign_id", campaignId),
  updateQueuePositions:  (updates) => supabase.from("post_campaigns").upsert(updates),
  getNextQueuePosition:  async () => { /* SELECT MAX(queue_position)+1 */ },
  uploadMedia:           (campaignId, file) =>
    supabase.storage.from("campaign-media")
      .upload(`${campaignId}/${file.name}`, file, { cacheControl: "3600", upsert: false }),
};
```

**`types/social-account.ts`**
```typescript
export interface SocialAccount {
  id: string;
  username: string;
  platform: "instagram" | "x" | "threads";
  browser_profile: string;
  is_active: boolean;
  last_used_at: string | null;
}
```

### 16.6 Shared Supabase Client

The client currently has no Supabase client setup. Create `client/lib/supabase.ts`:
```typescript
import { createClient } from "@supabase/supabase-js";

export const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
);
```

This is shared across both the post-bot and any future comment-bot client integration.

---

*This document covers the full post-bot backend. The system is architecturally identical to the comment-bot — refer to comment-bot.md for deeper context on shared patterns (Dolphin Anty, EventStore, media handling).*
