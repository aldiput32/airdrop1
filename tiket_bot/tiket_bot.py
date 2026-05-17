#!/usr/bin/env python3
"""
Tiket.com Auto Order Bot v2.0
- Bypass Cloudflare via CapSolver (AntiCloudflareTask / ProxyLess)
- Bypass reCAPTCHA Enterprise via CapSolver
- Auto fill order & proceed to payment
- Supports rotating proxy with sticky session workaround
"""

import json
import time
import re
import requests
import sys
import os
from datetime import datetime
from colorama import init, Fore, Style

init(autoreset=True)

# ============================================================
# UTILITY
# ============================================================

def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    colors = {
        "INFO": Fore.CYAN,
        "SUCCESS": Fore.GREEN,
        "ERROR": Fore.RED,
        "WARN": Fore.YELLOW,
        "STEP": Fore.MAGENTA
    }
    color = colors.get(level, Fore.WHITE)
    print(f"{Fore.WHITE}[{timestamp}] {color}[{level}]{Style.RESET_ALL} {msg}")


def load_config(path):
    with open(path, "r") as f:
        return json.load(f)


def load_order_data(path):
    data = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                data[key.strip()] = val.strip()
    return data


def get_proxy_url(config):
    """Build proxy URL for requests library"""
    p = config["proxy"]
    if not p["enabled"]:
        return None
    return f"http://{p['username']}:{p['password']}@{p['host']}:{p['port']}"


def get_capsolver_proxy(config):
    """Build proxy string for CapSolver API (format: host:port:user:pass)"""
    p = config["proxy"]
    if not p["enabled"]:
        return ""
    return f"{p['host']}:{p['port']}:{p['username']}:{p['password']}"


# ============================================================
# CAPSOLVER
# ============================================================

CAPSOLVER_API = "https://api.capsolver.com"


def capsolver_create_task(api_key, task_payload):
    """Generic create task + poll result"""
    payload = {
        "clientKey": api_key,
        "task": task_payload
    }

    try:
        resp = requests.post(f"{CAPSOLVER_API}/createTask", json=payload, timeout=30)
        result = resp.json()
    except Exception as e:
        log(f"API error: {e}", "ERROR")
        return None

    if result.get("errorId", 1) != 0:
        log(f"Task error: {result.get('errorDescription', 'Unknown')}", "ERROR")
        return None

    task_id = result["taskId"]
    log(f"Task ID: {task_id}", "INFO")

    # Poll
    for attempt in range(90):
        time.sleep(2)
        try:
            poll = requests.post(f"{CAPSOLVER_API}/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id
            }, timeout=30).json()
        except:
            continue

        status = poll.get("status", "")
        if status == "ready":
            return poll.get("solution", {})
        elif status == "failed":
            log(f"Task failed: {poll.get('errorDescription', '')}", "ERROR")
            return None

        if attempt % 10 == 0 and attempt > 0:
            log(f"Still solving... ({attempt * 2}s)", "INFO")

    log("Timeout waiting for solution", "ERROR")
    return None


def solve_cloudflare(config, proxy_url):
    """Solve Cloudflare Challenge - use host:port:user:pass format for CapSolver"""
    log("Solving Cloudflare Challenge...", "STEP")

    api_key = config["capsolver"]["api_key"]
    website_url = config["tiket"]["base_url"]
    capsolver_proxy = get_capsolver_proxy(config)

    log(f"Proxy for CapSolver: {capsolver_proxy[:30]}...", "INFO")

    task = {
        "type": "AntiCloudflareTask",
        "websiteURL": website_url,
        "proxy": capsolver_proxy,
        "metadata": {
            "type": "challenge"
        }
    }

    solution = capsolver_create_task(api_key, task)
    if solution:
        cookies = solution.get("cookies", {})
        ua = solution.get("userAgent", config["tiket"]["user_agent"])
        log(f"CF solved! Cookies: {list(cookies.keys())}", "SUCCESS")
        return {"cookies": cookies, "user_agent": ua}

    return None


def solve_recaptcha(config):
    """Solve reCAPTCHA Enterprise"""
    log("Solving reCAPTCHA Enterprise...", "STEP")

    api_key = config["capsolver"]["api_key"]
    task = {
        "type": "ReCaptchaV2EnterpriseTaskProxyLess",
        "websiteURL": config["capsolver"]["recaptcha_page_url"],
        "websiteKey": config["capsolver"]["recaptcha_site_key"],
        "isInvisible": True,
        "enterprisePayload": {}
    }

    solution = capsolver_create_task(api_key, task)
    if solution:
        token = solution.get("gRecaptchaResponse", "")
        log(f"reCAPTCHA solved! Token: {token[:40]}...", "SUCCESS")
        return token
    return None


# ============================================================
# BOT CLASS
# ============================================================

class TiketBot:
    def __init__(self, config, order_data, proxy_url):
        self.config = config
        self.order = order_data
        self.session = requests.Session()
        self.base_url = config["tiket"]["base_url"]
        self.locale = config["tiket"]["locale"]
        self.proxy_url = proxy_url
        self.session_id = None
        self.device_id = None
        self.correlation_id = None
        self.order_id = None
        self.order_hash = None

        # Set proxy for all requests
        if proxy_url:
            self.session.proxies = {"http": proxy_url, "https": proxy_url}

    def setup(self, cf_solution):
        """Setup session headers and cookies"""
        log("Setting up browser session...", "STEP")

        ua = cf_solution.get("user_agent", self.config["tiket"]["user_agent"])

        self.session.headers.update({
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })

        # Apply CF cookies
        for name, value in cf_solution.get("cookies", {}).items():
            self.session.cookies.set(name, value, domain=".tiket.com")

        # Verify proxy IP
        try:
            ip_resp = requests.get("https://httpbin.org/ip",
                                   proxies=self.session.proxies, timeout=10)
            log(f"Bot IP: {ip_resp.json().get('origin', '?')}", "INFO")
        except:
            log("Could not verify IP", "WARN")

        log(f"Session ready. UA: {ua[:50]}...", "SUCCESS")

    def _get(self, url, **kwargs):
        """GET with error handling"""
        try:
            resp = self.session.get(url, timeout=self.config["settings"]["timeout_sec"], **kwargs)
            return resp
        except requests.exceptions.ProxyError as e:
            log(f"Proxy error: {e}", "ERROR")
            return None
        except requests.exceptions.Timeout:
            log("Request timeout", "ERROR")
            return None
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return None

    def _post(self, url, **kwargs):
        """POST with error handling"""
        try:
            resp = self.session.post(url, timeout=self.config["settings"]["timeout_sec"], **kwargs)
            return resp
        except requests.exceptions.ProxyError as e:
            log(f"Proxy error: {e}", "ERROR")
            return None
        except requests.exceptions.Timeout:
            log("Request timeout", "ERROR")
            return None
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return None

    def step1_packages(self):
        """Step 1: Load packages page - resolves CF inline if needed"""
        log("[1/5] Loading packages page...", "STEP")
        slug = self.order["slug"]
        url = f"{self.base_url}/{self.locale}/to-do/{slug}/packages"

        resp = self._get(url)
        if not resp:
            return False

        log(f"Status: {resp.status_code}", "INFO")

        if resp.status_code == 403:
            log("BLOCKED (403) - Rotating proxy IP mismatch with CF cookie", "ERROR")
            log("Retrying: Solve CF again with fresh proxy IP...", "WARN")
            # Re-solve CF - CapSolver will use same rotating proxy, 
            # and we immediately use the new cookie
            cf = solve_cloudflare(self.config, self.proxy_url)
            if cf:
                self.setup(cf)
                # Retry immediately
                resp = self._get(url)
                if resp and resp.status_code == 200:
                    log(f"Status after re-solve: {resp.status_code}", "SUCCESS")
                else:
                    log("Still blocked after re-solve. Rotating proxy incompatible.", "ERROR")
                    log("TIP: Use a STATIC/STICKY proxy, not rotating.", "WARN")
                    return False
            else:
                return False

        if resp and resp.status_code == 200:
            # Parse __NEXT_DATA__
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text)
            if match:
                try:
                    nd = json.loads(match.group(1))
                    mw = nd.get("props", {}).get("pageProps", {}).get("middlewareData", {})
                    sd = mw.get("sessionData", {})
                    self.session_id = sd.get("sessionId", "")
                    self.device_id = mw.get("deviceId", "")
                    self.correlation_id = mw.get("correlationId", "")
                    log(f"Session: {self.session_id}", "SUCCESS")
                    log(f"Device:  {self.device_id}", "SUCCESS")
                except:
                    log("Could not parse __NEXT_DATA__", "WARN")
            return True

        log(f"Unexpected status: {resp.status_code if resp else 'None'}", "ERROR")
        return False

    def step2_order_page(self):
        """Step 2: Load order form"""
        log("[2/5] Loading order page...", "STEP")
        slug = self.order["slug"]
        url = f"{self.base_url}/{self.locale}/to-do/{slug}/order"

        self.session.headers["Referer"] = f"{self.base_url}/{self.locale}/to-do/{slug}/packages"
        self.session.headers["Sec-Fetch-Site"] = "same-origin"

        resp = self._get(url)
        if not resp:
            return False

        log(f"Status: {resp.status_code}", "INFO")
        if resp.status_code == 200:
            log("Order page loaded", "SUCCESS")
            return True

        log(f"Failed: {resp.status_code}", "ERROR")
        return False

    def step3_submit(self, recaptcha_token):
        """Step 3: Submit order"""
        log("[3/5] Submitting order...", "STEP")

        slug = self.order["slug"]
        payload = {
            "contactDetails": {
                "salutation": self.order.get("salutation", "Mr"),
                "fullname": self.order.get("fullname", ""),
                "phoneCountryCode": self.order.get("phone_country_code", "+62"),
                "phoneNumber": self.order.get("phone_number", ""),
                "emailAddress": self.order.get("email", ""),
                "country": self.order.get("country", "Indonesia")
            },
            "visitorDetails": [{
                "fullname": self.order.get("visitor_fullname", self.order.get("fullname", "")),
                "phoneNumber": self.order.get("visitor_phone", self.order.get("phone_number", "")),
                "email": self.order.get("visitor_email", self.order.get("email", ""))
            }],
            "recaptchaToken": recaptcha_token,
            "slug": slug,
            "quantity": int(self.order.get("quantity", 1)),
            "eventDate": self.order.get("event_date", ""),
            "ticketType": self.order.get("ticket_type", "Pax"),
            "paymentMethod": self.order.get("payment_method", "va_bca"),
            "currency": "IDR",
            "locale": self.locale
        }

        self.session.headers.update({
            "Content-Type": "application/json",
            "X-Session-Id": self.session_id or "",
            "X-Device-Id": self.device_id or "",
            "X-Correlation-Id": self.correlation_id or "",
            "X-Currency": "IDR",
            "X-Locale": "id",
            "Referer": f"{self.base_url}/{self.locale}/to-do/{slug}/order",
            "Origin": self.base_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })

        api_url = f"{self.base_url}/api/order/submit"
        resp = self._post(api_url, json=payload)
        if not resp:
            return False

        log(f"Status: {resp.status_code}", "INFO")

        if resp.status_code in [200, 201, 302]:
            try:
                data = resp.json()
                self.order_id = str(data.get("orderId", data.get("order_id", "")))
                self.order_hash = data.get("orderHash", data.get("order_hash", ""))
                log(f"Order ID: {self.order_id}", "SUCCESS")
                log(f"Hash: {self.order_hash}", "SUCCESS")
            except:
                loc = resp.headers.get("Location", "")
                if loc:
                    m = re.search(r'order_id=(\d+)', loc)
                    if m: self.order_id = m.group(1)
                    m2 = re.search(r'order_hash=([A-F0-9]+)', loc)
                    if m2: self.order_hash = m2.group(1)
                log(f"Response (no JSON): {resp.text[:200]}", "WARN")
            return True

        log(f"Failed: {resp.status_code} - {resp.text[:200]}", "ERROR")
        return False

    def step4_payment(self):
        """Step 4: Payment page"""
        log("[4/5] Processing payment...", "STEP")

        params = ""
        if self.order_id and self.order_hash:
            params = f"?order_id={self.order_id}&order_hash={self.order_hash}"

        url = f"{self.base_url}/{self.locale}/payment{params}"
        self.session.headers["Sec-Fetch-Dest"] = "document"
        self.session.headers["Sec-Fetch-Mode"] = "navigate"

        resp = self._get(url)
        if not resp:
            return False

        log(f"Status: {resp.status_code}", "INFO")
        if resp.status_code == 200:
            log("Payment page loaded", "SUCCESS")
            return True

        log(f"Failed: {resp.status_code}", "ERROR")
        return False

    def step5_confirm(self):
        """Step 5: Confirm and get VA"""
        log("[5/5] Confirming...", "STEP")

        params = ""
        if self.order_id and self.order_hash:
            params = f"?order_id={self.order_id}&order_hash={self.order_hash}"

        pm = self.order.get("payment_method", "va_bca")
        url = f"{self.base_url}/{self.locale}/payment/{pm}/confirm{params}"

        resp = self._get(url)
        if not resp:
            return False

        log(f"Status: {resp.status_code}", "INFO")
        if resp.status_code == 200:
            va = re.search(r'(\d{3}\s\d{4}\s\d{4}\s\d{4})', resp.text)
            if va:
                log(f"BCA VA: {va.group(1)}", "SUCCESS")
            total = re.search(r'IDR\s*([\d.,]+)', resp.text)
            if total:
                log(f"Total: IDR {total.group(1)}", "INFO")
            return True

        return False


# ============================================================
# MAIN
# ============================================================

def main():
    print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════╗
║  TIKET.COM AUTO ORDER BOT v2.0                   ║
║  Cloudflare + reCAPTCHA Bypass (CapSolver)        ║
║  Sticky Session Proxy Support                    ║
╚══════════════════════════════════════════════════╝{Style.RESET_ALL}
""")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config = load_config(os.path.join(script_dir, "config.json"))
    order_data = load_order_data(os.path.join(script_dir, "order_data.txt"))

    print(f"  {Fore.WHITE}Product : {Fore.YELLOW}{order_data.get('package_name', order_data.get('slug', '?'))}")
    print(f"  {Fore.WHITE}Qty     : {Fore.YELLOW}{order_data.get('quantity', '?')} pax")
    print(f"  {Fore.WHITE}Date    : {Fore.YELLOW}{order_data.get('event_date', '?')}")
    print(f"  {Fore.WHITE}Contact : {Fore.YELLOW}{order_data.get('fullname', '?')} <{order_data.get('email', '?')}>")
    print(f"  {Fore.WHITE}Payment : {Fore.YELLOW}{order_data.get('payment_method', '?')}")
    print()

    delay = config["settings"]["delay_between_steps_sec"]
    max_retries = config["settings"]["max_retries"]

    # === Get sticky proxy URL (same IP for entire session) ===
    proxy_url = get_proxy_url(config)
    if proxy_url:
        log(f"Proxy: {config['proxy']['host']}:{config['proxy']['port']} (sticky session)", "INFO")

        # Test proxy
        try:
            ip = requests.get("https://httpbin.org/ip", proxies={"http": proxy_url, "https": proxy_url}, timeout=10).json()["origin"]
            log(f"Proxy IP: {ip}", "SUCCESS")
        except Exception as e:
            log(f"Proxy test failed: {e}", "ERROR")
            sys.exit(1)
    print()

    # === SOLVE CLOUDFLARE ===
    cf_solution = None
    for retry in range(max_retries):
        cf_solution = solve_cloudflare(config, proxy_url)
        if cf_solution:
            break
        log(f"CF retry {retry+1}/{max_retries}...", "WARN")
        time.sleep(5)

    if not cf_solution:
        log("FATAL: Cannot bypass Cloudflare.", "ERROR")
        sys.exit(1)

    time.sleep(delay)

    # === INIT BOT ===
    bot = TiketBot(config, order_data, proxy_url)
    bot.setup(cf_solution)
    time.sleep(delay)

    # === STEP 1 ===
    success = False
    for retry in range(max_retries):
        if bot.step1_packages():
            success = True
            break
        log(f"Step 1 retry {retry+1}/{max_retries}...", "WARN")
        # Re-solve CF on 403
        cf_solution = solve_cloudflare(config, proxy_url)
        if cf_solution:
            bot.setup(cf_solution)
        time.sleep(3)

    if not success:
        log("FATAL: Cannot load packages page.", "ERROR")
        sys.exit(1)
    time.sleep(delay)

    # === STEP 2 ===
    if not bot.step2_order_page():
        log("FATAL: Cannot load order page.", "ERROR")
        sys.exit(1)
    time.sleep(delay)

    # === SOLVE RECAPTCHA ===
    recaptcha_token = None
    for retry in range(max_retries):
        recaptcha_token = solve_recaptcha(config)
        if recaptcha_token:
            break
        log(f"reCAPTCHA retry {retry+1}/{max_retries}...", "WARN")
        time.sleep(5)

    if not recaptcha_token:
        log("FATAL: Cannot solve reCAPTCHA.", "ERROR")
        sys.exit(1)
    time.sleep(delay)

    # === STEP 3 ===
    if not bot.step3_submit(recaptcha_token):
        log("Order submit had issues, continuing...", "WARN")
    time.sleep(delay)

    # === STEP 4 ===
    bot.step4_payment()
    time.sleep(delay)

    # === STEP 5 ===
    bot.step5_confirm()

    print()
    log("═" * 50, "SUCCESS")
    log("  DONE! Check your email for order details.", "SUCCESS")
    log("═" * 50, "SUCCESS")
    print()


if __name__ == "__main__":
    main()
