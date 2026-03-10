"""
DolphinAntyClient
-----------------
Manages Dolphin Anty browser profile sessions via its local REST API.
The local API runs at http://localhost:3001 while the Dolphin Anty desktop app is open.

Session flow:
  1. login()                → authenticate with API token
  2. find_profile_by_name() → resolve profile name → profile_id
  3. start_profile()        → launch browser, wait for CDP readiness, return automation info
  4. Caller connects:  chromium.connect_over_cdp(f"ws://{host}:{port}{wsEndpoint}")
  5. stop_profile()         → stop the browser when done
"""

import os
import socket
import time
import requests


class DolphinAntyClient:
    """Client for managing Dolphin Anty browser profiles"""

    def __init__(self):
        self.token = os.getenv('DOLPHIN_API_TOKEN')
        # Dolphin Anty local API - ensure it ends with /v1.0
        local_url = os.getenv('DOLPHIN_LOCAL_API_URL', 'http://localhost:3001')
        self.local_api_url = local_url.rstrip('/') + '/v1.0' if not local_url.endswith('/v1.0') else local_url
        self.public_api_url = 'https://dolphin-anty-api.com'
        self.headers = {'Content-Type': 'application/json'}
        self.api_headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
        # Extract the host from local_api_url for port checking
        # This is CRITICAL: port checks must target the Dolphin Anty server, not localhost
        from urllib.parse import urlparse
        parsed = urlparse(local_url)
        self.dolphin_host = parsed.hostname or 'localhost'
        print(f'[CONFIG] Dolphin Anty host for port checks: {self.dolphin_host}')

    def login(self, show_progress: bool = True) -> bool:
        """Login to Dolphin Anty with token

        Args:
            show_progress: Whether to show progress messages (default: True)
        """
        if show_progress:
            print(f'🔗 Connecting to Dolphin Anty at: {self.local_api_url}')

        try:
            response = requests.post(
                f'{self.local_api_url}/auth/login-with-token',
                json={'token': self.token},
                headers=self.headers,
                timeout=10
            )
            if response.status_code == 200:
                if show_progress:
                    print('[OK] Dolphin Anty login successful\n')
                return True
            if show_progress:
                print('[ERROR] Anti-detect browser authentication failed\n')
            return False
        except requests.exceptions.ConnectionError:
            if show_progress:
                print('[ERROR] Cannot connect to Dolphin Anty - make sure it is running\n')
            return False
        except Exception as e:
            if show_progress:
                print(f'[ERROR] Anti-detect browser connection failed: {e}\n')
            return False

    def get_profiles(self, limit: int = None) -> list:
        """Get list of browser profiles

        Args:
            limit: Maximum number of profiles to return (default: all)
        """
        url = f'{self.public_api_url}/browser_profiles'
        if limit:
            url += f'?limit={limit}'
        response = requests.get(
            url,
            headers=self.api_headers
        )
        if response.status_code == 200:
            return response.json().get('data', [])
        return []

    def find_profile_by_name(self, profile_name: str) -> dict | None:
        """Find a browser profile by its name

        Args:
            profile_name: The name of the browser profile to find

        Returns:
            Profile dict if found, None otherwise
        """
        try:
            # Fetch all profiles (Dolphin Anty API doesn't support name filtering)
            response = requests.get(
                f'{self.public_api_url}/browser_profiles',
                headers=self.api_headers
            )
            if response.status_code == 200:
                profiles = response.json().get('data', [])
                # Search for exact match (case-sensitive)
                for profile in profiles:
                    if profile.get('name') == profile_name:
                        return profile
                # If no exact match, try case-insensitive
                profile_name_lower = profile_name.lower()
                for profile in profiles:
                    if profile.get('name', '').lower() == profile_name_lower:
                        return profile
            return None
        except Exception as e:
            print(f'[ERR] Error finding profile by name: {e}')
            return None

    def find_profile_by_id(self, profile_id: str | int) -> dict | None:
        """Find a browser profile by its ID

        Args:
            profile_id: The ID of the browser profile to find (can be string or int)

        Returns:
            Profile dict if found, None otherwise
        """
        try:
            # Convert to int for comparison
            search_id = int(profile_id) if isinstance(profile_id, str) else profile_id

            # Fetch all profiles
            response = requests.get(
                f'{self.public_api_url}/browser_profiles',
                headers=self.api_headers
            )
            if response.status_code == 200:
                profiles = response.json().get('data', [])
                # Search for matching ID
                for profile in profiles:
                    if profile.get('id') == search_id:
                        return profile
            return None
        except Exception as e:
            print(f'[ERR] Error finding profile by ID: {e}')
            return None

    def _wait_for_port(self, port: int, host: str = None, timeout: int = 30) -> bool:
        """
        Wait until a port is open and accepting connections.

        This is a minimal readiness check to ensure the browser process
        has actually bound to the expected port before we attempt CDP connection.

        IMPORTANT: Uses self.dolphin_host by default to check the remote Dolphin Anty
        server, not localhost (which would be the Render server).

        Args:
            port: Port number to check
            host: Hostname (default: self.dolphin_host - the Dolphin Anty server)
            timeout: Maximum seconds to wait

        Returns:
            True if port is open, False if timeout reached
        """
        # Use Dolphin Anty host by default, not localhost
        if host is None:
            host = self.dolphin_host

        print(f'[INFO] Checking port {port} on host {host}')

        start_time = time.time()
        # Poll interval optimized for AWS Lightsail 2GB instances
        # 0.75s balances responsiveness with avoiding excessive CPU usage
        poll_interval = 0.75
        last_log_time = 0

        while time.time() - start_time < timeout:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)  # Short timeout for each probe
                result = sock.connect_ex((host, port))
                sock.close()

                if result == 0:
                    # Port is open
                    elapsed = int(time.time() - start_time)
                    print(f'[OK] Port {port} is now open and ready (took {elapsed}s)')
                    return True

            except socket.error:
                pass  # Port not ready yet

            # Log progress every 10 seconds for visibility
            elapsed = int(time.time() - start_time)
            if elapsed > 0 and elapsed % 10 == 0 and elapsed != last_log_time:
                print(f'[WAIT] Still waiting for port {port}... ({elapsed}s/{timeout}s)')
                last_log_time = elapsed

            time.sleep(poll_interval)

        return False

    def _verify_cdp_ready(self, port: int, host: str = None, timeout: int = 10) -> bool:
        """
        Verify that the Chrome DevTools Protocol endpoint is responsive.

        Sends a simple HTTP request to the CDP JSON endpoint to confirm
        the browser is ready to accept automation connections.

        IMPORTANT: Uses self.dolphin_host by default to check the remote Dolphin Anty
        server, not localhost (which would be the Render server).

        Args:
            port: CDP port number
            host: Hostname (default: self.dolphin_host - the Dolphin Anty server)
            timeout: Request timeout in seconds

        Returns:
            True if CDP endpoint responds, False otherwise
        """
        # Use Dolphin Anty host by default, not localhost
        if host is None:
            host = self.dolphin_host

        try:
            # CDP exposes a JSON endpoint that lists available debugging targets
            cdp_url = f'http://{host}:{port}/json/version'
            print(f'[INFO] Checking CDP endpoint at {cdp_url}')
            response = requests.get(cdp_url, timeout=timeout)

            if response.status_code == 200:
                # Optionally verify response contains expected CDP info
                data = response.json()
                if 'webSocketDebuggerUrl' in data or 'Browser' in data:
                    return True
                # Even without expected fields, a 200 response means CDP is up
                return True

        except requests.exceptions.RequestException:
            pass  # CDP not ready or unreachable
        except Exception:
            pass  # Unexpected error, treat as not ready

        return False

    def is_profile_running(self, profile_id: int) -> bool:
        """
        Check if a browser profile is currently running.

        Args:
            profile_id: The ID of the browser profile to check

        Returns:
            True if profile is running, False otherwise
        """
        try:
            # Dolphin Anty provides an endpoint to check active profiles
            response = requests.get(
                f'{self.local_api_url}/browser_profiles/{profile_id}/active',
                headers=self.headers,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                # Check if the profile has automation info (means it's running)
                return data.get('success', False) and data.get('automation') is not None

            return False

        except Exception as e:
            print(f'[WARN] Could not check if profile {profile_id} is running: {e}')
            return False

    def ensure_profile_stopped(self, profile_id: int) -> bool:
        """
        Ensure a profile is fully stopped before starting it.
        Checks if running and stops it, then waits for cleanup.

        Args:
            profile_id: The ID of the browser profile to stop

        Returns:
            True if profile is confirmed stopped, False if stop failed
        """
        try:
            # First check if profile is running
            if self.is_profile_running(profile_id):
                print(f'[INFO] Profile {profile_id} is currently running - stopping it first...')
                self.stop_profile(profile_id)
                time.sleep(3)  # Wait for cleanup

                # Verify it stopped
                if self.is_profile_running(profile_id):
                    print(f'[WARN] Profile {profile_id} still running after stop request')
                    # Try force stop one more time
                    self.stop_profile(profile_id)
                    time.sleep(2)
                    return not self.is_profile_running(profile_id)
                else:
                    print(f'[OK] Profile {profile_id} stopped successfully')
                    return True
            else:
                print(f'[OK] Profile {profile_id} is not running - ready to start')
                return True

        except Exception as e:
            print(f'[WARN] Error checking/stopping profile {profile_id}: {e}')
            # Try to stop anyway as a safety measure
            self.stop_profile(profile_id)
            time.sleep(2)
            return True  # Assume it worked

    def start_profile(self, profile_id: int, headless: bool = None,
                      max_retries: int = 3, startup_timeout: int = 120) -> dict | None:
        """
        Start a browser profile using REST API with readiness verification.

        This method implements a deterministic startup sequence:
        1. Call the Dolphin Anty REST endpoint to start the profile
        2. Initial grace period (10s) to allow browser process to start binding to port
        3. Wait for the returned port to be open (browser process started)
        4. Verify the CDP endpoint is responsive (browser ready for automation)
        5. Return automation info only after readiness is confirmed

        Retry logic handles transient failures (timeouts, port not ready).
        Permanent errors (401, 403, 404) fail immediately.

        CRITICAL FIX: Extended timeouts and initial delay to fix port binding race
        condition on AWS Lightsail 2GB instances where browser startup is slower.

        Timing configuration:
        - startup_timeout: 120s (total timeout for entire startup sequence)
        - initial_delay: 10s (grace period BEFORE first port check - CRITICAL)
        - port_timeout: 90s (max wait for port to become available)
        - cdp_timeout: 20s (max wait for CDP endpoint to respond)
        - retry_cooldown: 8s (pause between retry attempts)
        - poll_interval: 0.75s (frequency of port availability checks)

        Args:
            profile_id: The ID of the browser profile to start
            headless: Run in headless mode. If None, defaults to True (always headless).
            max_retries: Number of retry attempts on transient failures (default: 3)
            startup_timeout: Max seconds to wait for browser readiness (default: 120)

        Returns:
            Automation info dict with port and wsEndpoint, or None on failure
        """
        # Default to headless mode (always run headless unless explicitly set to False)
        if headless is None:
            headless = True

        # =============================================================
        # PRE-START: Ensure profile is stopped before starting
        # =============================================================
        # This prevents 500 errors from trying to start an already-running profile
        print(f'[CHECK] Checking if profile {profile_id} is already running...')
        if not self.ensure_profile_stopped(profile_id):
            print(f'[WARN] Could not confirm profile {profile_id} is stopped - proceeding anyway')

        # Build base URL (port will be auto-assigned by Dolphin Anty)
        # Adding a random component ensures we get a fresh port allocation each time
        base_url = f'{self.local_api_url}/browser_profiles/{profile_id}/start?automation=1'
        if headless:
            base_url += '&headless=true'

        last_error = None

        for attempt in range(max_retries):
            try:
                # =============================================================
                # STEP 1: Call REST API to start the profile
                # =============================================================
                # Add cache-busting parameter to ensure fresh port allocation on each attempt
                url = f'{base_url}&_t={int(time.time() * 1000)}'
                print(f'[INFO] Attempt {attempt + 1}/{max_retries}: Requesting new browser instance...')

                response = requests.get(url, headers=self.headers, timeout=30)

                # Handle permanent errors - fail fast, no retry
                if response.status_code == 401:
                    print(f'[ERR] Authentication failed (401) - check API token')
                    return None
                if response.status_code == 403:
                    print(f'[ERR] Access forbidden (403) - insufficient permissions')
                    return None
                if response.status_code == 404:
                    print(f'[ERR] Profile not found (404) - profile ID {profile_id} does not exist')
                    return None

                # Handle non-200 responses as transient errors
                if response.status_code != 200:
                    # Try to extract error details from response body
                    error_details = ''
                    try:
                        error_data = response.json()
                        error_details = error_data.get('error', error_data.get('message', ''))
                    except Exception:
                        error_details = response.text[:200] if response.text else ''

                    last_error = f'REST API returned status {response.status_code}'
                    print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                    if error_details:
                        print(f'[WARN] Dolphin Anty error: {error_details}')

                    # Detect Windows file lock errors - these won't resolve with retries
                    file_lock_keywords = ['EBUSY', 'resource busy', 'locked', 'UNKNOWN: unknown error, open']
                    is_file_lock_error = any(keyword in error_details for keyword in file_lock_keywords)

                    if is_file_lock_error:
                        print(f'[ERR] Windows file lock detected on profile {profile_id}!')
                        print(f'[INFO] Attempting automatic cleanup...')

                        # Attempt auto-cleanup on Windows before giving up
                        import platform as _platform
                        if _platform.system() == "Windows":
                            import subprocess
                            default_dir = (
                                f"C:\\Users\\Administrator\\AppData\\Roaming\\"
                                f"dolphin_anty\\browser_profiles\\{profile_id}\\data_dir\\Default"
                            )
                            try:
                                result = subprocess.run(
                                    ["powershell", "-Command",
                                     f'Remove-Item -Recurse -Force "{default_dir}" -ErrorAction Stop'],
                                    capture_output=True, text=True, timeout=15
                                )
                                if result.returncode == 0:
                                    print(f'[OK] Auto-cleaned profile {profile_id} Default directory')
                                    print(f'[INFO] Retrying profile start...')
                                    time.sleep(3)
                                    continue  # Retry within the for attempt loop
                                else:
                                    print(f'[WARN] Auto-cleanup failed: {result.stderr.strip()}')
                            except Exception as e:
                                print(f'[WARN] Auto-cleanup exception: {e}')

                        # If auto-cleanup failed or not on Windows, fall through to manual instructions
                        print(f'[ERR] Manual fix required — run on Windows server:')
                        print(f'[ERR]   Remove-Item -Recurse -Force "C:\\Users\\Administrator\\AppData\\Roaming\\dolphin_anty\\browser_profiles\\{profile_id}\\data_dir\\Default"')
                        print(f'[ERR] Or assign a different browser profile to this account.')
                        return None

                    # On 500 error, profile might be in bad state - try stopping it first
                    if response.status_code == 500:
                        print(f'[INFO] Attempting to stop profile {profile_id} before retry (may be stuck)...')
                        self.stop_profile(profile_id)
                        time.sleep(3)  # Give it time to fully stop

                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                    continue

                # Parse response
                data = response.json()
                if not data.get('success'):
                    error_msg = data.get('error', 'Unknown error from Dolphin Anty')
                    last_error = f'Profile start failed: {error_msg}'
                    print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                    continue

                automation_info = data.get('automation', {})
                port = automation_info.get('port')
                ws_endpoint = automation_info.get('wsEndpoint', 'N/A')

                if not port:
                    last_error = 'No port returned in automation info'
                    print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                    continue

                # Log the assigned port for debugging
                print(f'[OK] Dolphin Anty assigned port {port} for this session')
                print(f'[INFO] WebSocket endpoint: {ws_endpoint}')

                # =============================================================
                # STEP 2: Initial grace period for browser process startup
                # =============================================================
                # CRITICAL FIX: 10s delay to fix port binding race condition on AWS Lightsail
                # The browser process needs time to start and bind to the port before we check.
                # Without this delay, we check too early and timeout waiting for a port that
                # the browser hasn't had time to bind to yet.
                initial_delay = 10
                print(f'[WAIT] Allowing browser process {initial_delay}s to initialize...')
                time.sleep(initial_delay)

                # =============================================================
                # STEP 3: Check if running remotely (skip port check if browser binds to 127.0.0.1)
                # =============================================================
                # When Dolphin Anty runs on a remote Windows server, the browser often binds
                # to 127.0.0.1 only, making it inaccessible from our Render server.
                # In this case, we skip the port/CDP checks and trust Dolphin Anty's response.
                is_remote = self.dolphin_host != 'localhost' and self.dolphin_host != '127.0.0.1'

                if is_remote:
                    print(f'[INFO] Remote Dolphin Anty detected ({self.dolphin_host})')
                    print(f'[INFO] Skipping port check (browser likely binds to 127.0.0.1 on Windows)')
                    print(f'[INFO] Trusting Dolphin Anty response - port {port} should be ready')
                    # Give extra time for browser to fully initialize
                    extra_delay = 5
                    print(f'[WAIT] Additional {extra_delay}s wait for remote browser stability...')
                    time.sleep(extra_delay)
                    # Return immediately, trusting the automation info
                    print(f'[OK] Profile started successfully (remote mode)')
                    return automation_info

                # =============================================================
                # STEP 4: Wait for port to be open (LOCAL mode only)
                # =============================================================
                # Extended timeout for AWS Lightsail 2GB instances with slower I/O
                port_timeout = 90
                print(f'[CHECK] Waiting up to {port_timeout}s for port {port}...')
                if not self._wait_for_port(port, timeout=port_timeout):
                    last_error = f'Timeout waiting for port {port} to open after {port_timeout}s'
                    print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                    # Try to stop the profile before retrying
                    self.stop_profile(profile_id)
                    if attempt < max_retries - 1:
                        time.sleep(8)  # 8s cooldown between retries for AWS Lightsail
                    continue

                # =============================================================
                # STEP 5: Verify CDP endpoint is responsive (LOCAL mode only)
                # =============================================================
                # Extended CDP timeout for AWS Lightsail 2GB instances
                cdp_timeout = 20
                print(f'[CHECK] Verifying CDP endpoint is responsive (timeout: {cdp_timeout}s)...')
                if not self._verify_cdp_ready(port, timeout=cdp_timeout):
                    last_error = f'CDP endpoint not responsive on port {port}'
                    print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                    # Try to stop the profile before retrying
                    self.stop_profile(profile_id)
                    if attempt < max_retries - 1:
                        time.sleep(8)  # 8s cooldown between retries for AWS Lightsail
                    continue

                # =============================================================
                # SUCCESS: Profile started and browser is ready
                # =============================================================
                return automation_info

            except requests.exceptions.Timeout:
                last_error = 'Request timeout'
                print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

            except requests.exceptions.ConnectionError:
                last_error = 'Connection error - Dolphin Anty may not be running'
                print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

            except Exception as e:
                last_error = f'Unexpected error: {str(e)}'
                print(f'[WARN] Profile start attempt {attempt + 1}/{max_retries}: {last_error}')
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        # All retries exhausted
        print(f'[ERR] Failed to start profile {profile_id} after {max_retries} attempts: {last_error}')
        return None

    def stop_profile(self, profile_id: int) -> bool:
        """Stop a running browser profile"""
        try:
            response = requests.get(
                f'{self.local_api_url}/browser_profiles/{profile_id}/stop',
                headers=self.headers
            )
            return response.status_code == 200
        except Exception:
            return False
