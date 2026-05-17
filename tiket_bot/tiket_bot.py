#!/usr/bin/env python3
"""
Tiket.com Auto Order Bot
- Bypass Cloudflare WAF via CapSolver (AntiCloudflareTask)
- Bypass reCAPTCHA Enterprise via CapSolver
- Auto fill order form & proceed to payment
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
# UTILITY FUNCTIONS
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


# ============================================================
# CAPSOLVER FUNCTIONS
# ============================================================

CAPSOLVER_API = "https://api.capsolver.com"


def solve_cloudflare(config):
    """Solve Cloudflare Challenge via CapSolver AntiCloudflareTask"""
    log("Solving Cloudflare Challenge...", "STEP")

    api_key = config["capsolver"]["api_key"]
    proxy_cfg = config["proxy"]
    tiket_cfg = config["tiket"]

    proxy_str = ""
    if proxy_cfg["enabled"]:
        proxy_str = f"{proxy_cfg['type']}://{proxy_cfg['username']}:{proxy_cfg['password']}@{proxy_cfg['host']}:{proxy_cfg['port']}"

    payload = {
        "clientKey": api_key,
        "task": {
            "type": "AntiCloudflareTask",
            "websiteURL": tiket_cfg["base_url"],
            "proxy": proxy_str,
            "metadata": {
                "type": "challenge"
            }
        }
    }

    try:
        resp = requests.post(f"{CAPSOLVER_API}/createTask", json=payload, timeout=30)
        result = resp.json()
    except Exception as e:
        log(f"Cloudflare API error: {e}", "ERROR")
        return None

    if result.get("errorId", 1) != 0:
        log(f"Cloudflare task error: {result.get('errorDescription', 'Unknown')}", "ERROR")
        return None

    task_id = result["taskId"]
    log(f"Task ID: {task_id}", "INFO")

    for attempt in range(60):
        time.sleep(3)
        try:
            poll_resp = requests.post(f"{CAPSOLVER_API}/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id
            }, timeout=30)
            poll_result = poll_resp.json()
        except Exception as e:
            log(f"Poll error: {e}", "WARN")
            continue

        status = poll_result.get("status", "")
        if status == "ready":
            solution = poll_result.get("solution", {})
            log("Cloudflare SOLVED! cf_clearance obtained.", "SUCCESS")
            return {
                "cookies": solution.get("cookies", {}),
                "user_agent": solution.get("userAgent", tiket_cfg["user_agent"])
            }
        elif status == "failed":
            log(f"CF failed: {poll_result.get('errorDescription', '')}", "ERROR")
            return None
        else:
            if attempt % 5 == 0:
                log(f"Waiting... ({attempt * 3}s)", "INFO")

    log("Cloudflare solving timeout (180s)", "ERROR")
    return None


def solve_recaptcha_enterprise(config):
    """Solve reCAPTCHA Enterprise via CapSolver"""
    log("Solving reCAPTCHA Enterprise...", "STEP")

    api_key = config["capsolver"]["api_key"]
    site_key = config["capsolver"]["recaptcha_site_key"]
    page_url = config["capsolver"]["recaptcha_page_url"]

    payload = {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV2EnterpriseTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "isInvisible": True,
            "enterprisePayload": {}
        }
    }

    try:
        resp = requests.post(f"{CAPSOLVER_API}/createTask", json=payload, timeout=30)
        result = resp.json()
    except Exception as e:
        log(f"reCAPTCHA API error: {e}", "ERROR")
        return None

    if result.get("errorId", 1) != 0:
        log(f"reCAPTCHA task error: {result.get('errorDescription', 'Unknown')}", "ERROR")
        return None

    task_id = result["taskId"]
    log(f"Task ID: {task_id}", "INFO")

    for attempt in range(60):
        time.sleep(3)
        try:
            poll_resp = requests.post(f"{CAPSOLVER_API}/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id
            }, timeout=30)
            poll_result = poll_resp.json()
        except Exception as e:
            log(f"Poll error: {e}", "WARN")
            continue

        status = poll_result.get("status", "")
        if status == "ready":
            token = poll_result.get("solution", {}).get("gRecaptchaResponse", "")
            log(f"reCAPTCHA SOLVED! Token: {token[:40]}...", "SUCCESS")
            return token
        elif status == "failed":
            log(f"reCAPTCHA failed: {poll_result.get('errorDescription', '')}", "ERROR")
            return None
        else:
            if attempt % 5 == 0:
                log(f"Waiting... ({attempt * 3}s)", "INFO")

    log("reCAPTCHA solving timeout (180s)", "ERROR")
    return None


# ============================================================
# TIKET.COM BOT CLASS
# ============================================================

class TiketBot:
    def __init__(self, config, order_data):
        self.config = config
        self.order = order_data
        self.session = requests.Session()
        self.base_url = config["tiket"]["base_url"]
        self.locale = config["tiket"]["locale"]
        self.session_id = None
        self.device_id = None
        self.order_id = None
        self.order_hash = None

    def setup_session(self, cf_solution):
        """Setup session with Cloudflare cookies and headers"""
        log("Setting up session...", "STEP")

        ua = cf_solution.get("user_agent", self.config["tiket"]["user_agent"])

        self.session.headers.update({
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })

        # Set CF cookies
        for name, value in cf_solution.get("cookies", {}).items():
            self.session.cookies.set(name, value, domain=".tiket.com")

        # Proxy
        if self.config["proxy"]["enabled"]:
            p = self.config["proxy"]
            proxy_url = f"{p['type']}://{p['username']}:{p['password']}@{p['host']}:{p['port']}"
            self.session.proxies = {"http": proxy_url, "https": proxy_url}

        log(f"Session ready. UA: {ua[:50]}...", "SUCCESS")

    def _extract_next_data(self, html):
        """Extract __NEXT_DATA__ JSON from HTML"""
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if match:
            return json.loads(match.group(1))
        return None

    def step1_load_packages(self):
        """Load packages page, get session"""
        log("[1/5] Loading packages page...", "STEP")
        slug = self.order["slug"]
        url = f"{self.base_url}/{self.locale}/to-do/{slug}/packages"

        try:
            resp = self.session.get(url, timeout=self.config["settings"]["timeout_sec"])
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return False

        log(f"Status: {resp.status_code}", "INFO")

        if resp.status_code == 403:
            log("BLOCKED by Cloudflare (403)", "ERROR")
            return False

        if resp.status_code == 200:
            next_data = self._extract_next_data(resp.text)
            if next_data:
                mw = next_data.get("props", {}).get("pageProps", {}).get("middlewareData", {})
                sd = mw.get("sessionData", {})
                self.session_id = sd.get("sessionId", "")
                self.device_id = mw.get("deviceId", "")
                log(f"Session: {self.session_id}", "SUCCESS")
                log(f"Device: {self.device_id}", "SUCCESS")
            return True

        log(f"Unexpected: {resp.status_code}", "ERROR")
        return False

    def step2_load_order_page(self):
        """Load order form page"""
        log("[2/5] Loading order page...", "STEP")
        slug = self.order["slug"]
        url = f"{self.base_url}/{self.locale}/to-do/{slug}/order"

        self.session.headers.update({
            "Referer": f"{self.base_url}/{self.locale}/to-do/{slug}/packages"
        })

        try:
            resp = self.session.get(url, timeout=self.config["settings"]["timeout_sec"])
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return False

        log(f"Status: {resp.status_code}", "INFO")
        if resp.status_code == 200:
            log("Order page loaded OK", "SUCCESS")
            return True

        log(f"Failed: {resp.status_code}", "ERROR")
        return False

    def step3_submit_order(self, recaptcha_token):
        """Submit order with contact + visitor details"""
        log("[3/5] Submitting order...", "STEP")

        slug = self.order["slug"]

        order_payload = {
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
            "X-Currency": "IDR",
            "X-Locale": "id",
            "Referer": f"{self.base_url}/{self.locale}/to-do/{slug}/order",
            "Origin": self.base_url
        })

        # Try the internal Next.js API
        api_url = f"{self.base_url}/api/order/submit"

        try:
            resp = self.session.post(api_url, json=order_payload, timeout=self.config["settings"]["timeout_sec"])
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return False

        log(f"Status: {resp.status_code}", "INFO")

        if resp.status_code in [200, 201, 302]:
            try:
                data = resp.json()
                self.order_id = str(data.get("orderId", data.get("order_id", "")))
                self.order_hash = data.get("orderHash", data.get("order_hash", ""))
                log(f"Order ID: {self.order_id}", "SUCCESS")
                log(f"Order Hash: {self.order_hash}", "SUCCESS")
            except:
                # Maybe redirected, check for Location header
                if resp.headers.get("Location"):
                    loc = resp.headers["Location"]
                    log(f"Redirected to: {loc}", "INFO")
                    # Extract order_id from URL
                    m = re.search(r'order_id=(\d+)', loc)
                    if m:
                        self.order_id = m.group(1)
                    m2 = re.search(r'order_hash=([A-F0-9]+)', loc)
                    if m2:
                        self.order_hash = m2.group(1)
            return True
        else:
            log(f"Order failed: {resp.text[:300]}", "ERROR")
            return False

    def step4_payment(self):
        """Navigate to payment and select method"""
        log("[4/5] Processing payment...", "STEP")

        params = ""
        if self.order_id and self.order_hash:
            params = f"?order_id={self.order_id}&order_hash={self.order_hash}"

        url = f"{self.base_url}/{self.locale}/payment{params}"

        try:
            resp = self.session.get(url, timeout=self.config["settings"]["timeout_sec"])
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return False

        log(f"Payment page status: {resp.status_code}", "INFO")

        if resp.status_code == 200:
            log("Payment page loaded", "SUCCESS")

            # Select payment method via API
            payment_method = self.order.get("payment_method", "va_bca")
            pay_url = f"{self.base_url}/api/payment/select"
            pay_payload = {
                "orderId": self.order_id or "",
                "orderHash": self.order_hash or "",
                "paymentMethod": payment_method
            }

            try:
                resp2 = self.session.post(pay_url, json=pay_payload, timeout=30)
                log(f"Payment select status: {resp2.status_code}", "INFO")
            except:
                log("Payment select API not hit (may use form submit)", "WARN")

            return True

        log(f"Payment page error: {resp.status_code}", "ERROR")
        return False

    def step5_confirm(self):
        """Load confirmation page, get VA number"""
        log("[5/5] Confirming payment...", "STEP")

        params = ""
        if self.order_id and self.order_hash:
            params = f"?order_id={self.order_id}&order_hash={self.order_hash}"

        pm = self.order.get("payment_method", "va_bca")
        url = f"{self.base_url}/{self.locale}/payment/{pm}/confirm{params}"

        try:
            resp = self.session.get(url, timeout=self.config["settings"]["timeout_sec"])
        except Exception as e:
            log(f"Request error: {e}", "ERROR")
            return False

        log(f"Confirm page status: {resp.status_code}", "INFO")

        if resp.status_code == 200:
            # Extract VA number
            va_match = re.search(r'(\d{3}\s\d{4}\s\d{4}\s\d{4})', resp.text)
            if va_match:
                va_number = va_match.group(1)
                log(f"BCA Virtual Account: {va_number}", "SUCCESS")
            else:
                log("VA number not found in response (check email)", "WARN")

            # Extract total
            total_match = re.search(r'IDR\s*([\d.,]+)', resp.text)
            if total_match:
                log(f"Total: IDR {total_match.group(1)}", "INFO")

            return True

        log(f"Confirm page error: {resp.status_code}", "ERROR")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    banner = f"""
{Fore.CYAN}╔══════════════════════════════════════════════════╗
║  TIKET.COM AUTO ORDER BOT v1.0                   ║
║  Cloudflare + reCAPTCHA Enterprise Bypass         ║
║  Powered by CapSolver                            ║
╚══════════════════════════════════════════════════╝{Style.RESET_ALL}
"""
    print(banner)

    # Paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.json")
    order_path = os.path.join(script_dir, "order_data.txt")

    # Load
    log("Loading configuration...", "STEP")
    config = load_config(config_path)
    order_data = load_order_data(order_path)

    print(f"\n  {Fore.WHITE}Product : {Fore.YELLOW}{order_data.get('package_name', order_data.get('slug', 'N/A'))}")
    print(f"  {Fore.WHITE}Qty     : {Fore.YELLOW}{order_data.get('quantity', '?')} pax")
    print(f"  {Fore.WHITE}Date    : {Fore.YELLOW}{order_data.get('event_date', '?')}")
    print(f"  {Fore.WHITE}Contact : {Fore.YELLOW}{order_data.get('fullname', '?')} <{order_data.get('email', '?')}>")
    print(f"  {Fore.WHITE}Payment : {Fore.YELLOW}{order_data.get('payment_method', '?')}")
    print(f"  {Fore.WHITE}Proxy   : {Fore.YELLOW}{'ON' if config['proxy']['enabled'] else 'OFF'}")
    print()

    delay = config["settings"]["delay_between_steps_sec"]
    max_retries = config["settings"]["max_retries"]

    # ===== CLOUDFLARE BYPASS =====
    cf_solution = None
    for retry in range(max_retries):
        cf_solution = solve_cloudflare(config)
        if cf_solution:
            break
        log(f"Retry CF ({retry+1}/{max_retries})...", "WARN")
        time.sleep(5)

    if not cf_solution:
        log("FATAL: Cannot bypass Cloudflare after retries. Exiting.", "ERROR")
        sys.exit(1)

    time.sleep(delay)

    # ===== BOT INIT =====
    bot = TiketBot(config, order_data)
    bot.setup_session(cf_solution)
    time.sleep(delay)

    # ===== STEP 1 =====
    if not bot.step1_load_packages():
        log("FATAL: Cannot load packages. Exiting.", "ERROR")
        sys.exit(1)
    time.sleep(delay)

    # ===== STEP 2 =====
    if not bot.step2_load_order_page():
        log("FATAL: Cannot load order page. Exiting.", "ERROR")
        sys.exit(1)
    time.sleep(delay)

    # ===== RECAPTCHA SOLVE =====
    recaptcha_token = None
    for retry in range(max_retries):
        recaptcha_token = solve_recaptcha_enterprise(config)
        if recaptcha_token:
            break
        log(f"Retry reCAPTCHA ({retry+1}/{max_retries})...", "WARN")
        time.sleep(5)

    if not recaptcha_token:
        log("FATAL: Cannot solve reCAPTCHA. Exiting.", "ERROR")
        sys.exit(1)
    time.sleep(delay)

    # ===== STEP 3 =====
    if not bot.step3_submit_order(recaptcha_token):
        log("Order submit may have issues, continuing...", "WARN")
    time.sleep(delay)

    # ===== STEP 4 =====
    bot.step4_payment()
    time.sleep(delay)

    # ===== STEP 5 =====
    bot.step5_confirm()

    print()
    log("═" * 50, "SUCCESS")
    log("  BOT FINISHED! Check email for e-ticket.", "SUCCESS")
    log("═" * 50, "SUCCESS")
    print()


if __name__ == "__main__":
    main()
