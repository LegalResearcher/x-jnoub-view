import json
import os
import re
import sys
import time

import requests

# ══════════════ الإعدادات ══════════════
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
os.makedirs(STATE_DIR, exist_ok=True)

LINKS_PATH = os.path.join(STATE_DIR, "telegram_links.txt")        # طابور التنفيذ
PROCESSED_PATH = os.path.join(STATE_DIR, "processed_links.txt")   # روابط تم سحبها
DONE_PATH = os.path.join(STATE_DIR, "done_links.txt")             # روابط تم تنفيذها
ATTEMPTS_PATH = os.path.join(STATE_DIR, "failed_attempts.json")   # عدّاد الفشل لكل رابط

X_HANDLE = "aljanoubvoice2"   # حساب تويتر/X (بدون @)
# مرايا Nitter لقراءة الحساب (قابلة للتغيير من متغير البيئة NITTER_INSTANCES مفصولة بفواصل)
NITTER_INSTANCES = [
    h.strip()
    for h in os.environ.get("NITTER_INSTANCES", "nitter.net,xcancel.com,nitter.poast.org").split(",")
    if h.strip()
]

API_URL = "https://prince.services/api/v2"
SITE_URL = os.environ.get("SITE_URL", "https://prince.services")
SERVICE_ID = "9335"   # مشاهدات فيديو تويتر مجانية
QUANTITY = "500"      # الحد الأقصى للخدمة

# نصوص الواجهة (كما تظهر في الموقع)
CATEGORY_TEXT = "خدمات مجانية"
SERVICE_TEXT = "9335"
CONFIRM_TEXT = "تاكيد الطلب"
SUCCESS_TEXT = "تم استلام طلبك بنجاح"
DEBUG_DIR = os.path.join(ROOT, "debug")

MAX_ATTEMPTS_PER_LINK = 3   # بعد 3 إخفاقات يُتخطى الرابط نهائياً
MAX_TRIES_PER_RUN = 3       # أقصى عدد طلبات في التشغيل الواحد

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ══════════════ ملفات ══════════════
def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def append_lines(path, lines):
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def load_attempts():
    if not os.path.exists(ATTEMPTS_PATH):
        return {}
    try:
        with open(ATTEMPTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def save_attempts(data):
    with open(ATTEMPTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════ السحب ══════════════
def tweet_link(tweet_id):
    return f"https://x.com/{X_HANDLE}/status/{tweet_id}"


def ids_from_x_profile():
    """المصدر المباشر: صفحة الحساب العامة على X دون الاعتماد على Nitter."""
    url = f"https://x.com/{X_HANDLE}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"كود الحالة {r.status_code}")
    ids = []
    pattern = rf"/{re.escape(X_HANDLE)}/status/(\d+)"
    for value in re.findall(pattern, r.text, re.I):
        if value not in ids:
            ids.append(value)
    if not ids:
        raise RuntimeError("لم توجد روابط منشورات في صفحة الحساب")
    return {int(value) for value in ids}


def ids_from_syndication():
    """المصدر 1: واجهة العرض المضمّن (Embed) من تويتر."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{X_HANDLE}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"كود الحالة {r.status_code}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("لا توجد بيانات في الصفحة")
    try:
        entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    except (KeyError, ValueError, TypeError):
        raise RuntimeError("شكل البيانات غير متوقع")

    ids = set()
    for e in entries:
        if e.get("type") != "tweet":
            continue
        t = (e.get("content") or {}).get("tweet") or {}
        user = ((t.get("user") or {}).get("screen_name") or "").lower()
        if user != X_HANDLE.lower():
            continue
        if t.get("retweeted_status") or t.get("in_reply_to_status_id_str"):
            continue  # نتجاهل إعادة التغريد والردود
        if t.get("id_str"):
            ids.add(int(t["id_str"]))
    return ids


def ids_from_nitter(host):
    """المصدر 2: RSS من مرآة Nitter."""
    url = f"https://{host}/{X_HANDLE}/rss"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"كود الحالة {r.status_code}")
    if "<item>" not in r.text:
        raise RuntimeError("لا توجد عناصر في الـ RSS")

    ids = set()
    for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        tm = re.search(r"<title>(.*?)</title>", item, re.S)
        title = re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)).strip() if tm else ""
        if title.startswith("RT by") or title.startswith("R to"):
            continue  # إعادة تغريد أو رد
        m = re.search(rf"/{re.escape(X_HANDLE)}/status/(\d+)", item, re.I)
        if m:
            ids.add(int(m.group(1)))
    return ids


def fetch_links():
    """روابط التغريدات الأصلية للحساب، من الأقدم للأحدث. يجرّب المصادر بالترتيب."""
    sources = [("x-profile", ids_from_x_profile), ("syndication", ids_from_syndication)]
    sources += [(f"nitter:{h}", (lambda h=h: ids_from_nitter(h))) for h in NITTER_INSTANCES]

    errors = []
    for name, fn in sources:
        try:
            ids = fn()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if ids:
            log(f"📡 المصدر: {name} ({len(ids)} تغريدة)")
            return [tweet_link(i) for i in sorted(ids)]
        errors.append(f"{name}: لا توجد تغريدات")

    raise RuntimeError("فشلت كل المصادر | " + " | ".join(errors))


def pull_once():
    first_run = not os.path.exists(PROCESSED_PATH)
    discovered = fetch_links()
    if not discovered:
        log("⏹️ لم يتم العثور على منشورات في الصفحة.")
        return

    processed = set(read_lines(PROCESSED_PATH))
    new_links = [l for l in discovered if l not in processed]
    if not new_links:
        log("🔎 لا توجد منشورات جديدة.")
        return

    if first_run:
        # أول تشغيل: الأحدث فقط، وباقي ما في الصفحة يُسجَّل كمعالج دون تنفيذ
        append_lines(LINKS_PATH, [new_links[-1]])
        append_lines(PROCESSED_PATH, new_links)
        log(f"🆕 أول تشغيل، أُضيف أحدث رابط فقط: {new_links[-1]}")
        if len(new_links) > 1:
            log(f"ℹ️ تم تجاهل {len(new_links) - 1} منشور أقدم.")
        return

    # الأشغال العادية: كل الجديد بالترتيب (الأقدم أولاً)
    append_lines(LINKS_PATH, new_links)
    append_lines(PROCESSED_PATH, new_links)
    log(f"✨ أُضيف {len(new_links)} رابط جديد إلى الطابور.")


# ══════════════ التنفيذ ══════════════
def send_order(api_key, link):
    """يرجع: 'ok' | 'fatal' (مفتاح/رصيد) | 'fail'."""
    payload = {
        "key": api_key,
        "action": "add",
        "service": SERVICE_ID,
        "link": link,
        "quantity": QUANTITY,
    }
    try:
        response = requests.post(API_URL, data=payload, timeout=30)
    except requests.RequestException as e:
        log(f"⚠️ خطأ اتصال: {type(e).__name__}")
        return "fail"

    if response.status_code == 401:
        log("❌ المفتاح مرفوض (401).")
        return "fatal"
    if response.status_code != 200:
        log(f"❌ كود الحالة: {response.status_code}")
        return "fail"

    try:
        result = response.json()
    except ValueError:
        log("⚠️ رد غير متوقع من السيرفر (ليس JSON).")
        return "fail"

    if "order" in result:
        log(f"✅ تم الطلب. رقم المعاملة: {result['order']}")
        return "ok"

    error = str(result.get("error", "غير معروف"))
    log(f"⚠️ السيرفر أعاد خطأ: {error}")
    if "key" in error.lower() or "balance" in error.lower():
        return "fatal"
    return "fail"



# ══════════════ التنفيذ عبر المتصفح ══════════════
SELECT_JS = """
(needle) => {
  for (const s of Array.from(document.querySelectorAll('select'))) {
    for (const o of s.options) {
      if (o.text.includes(needle)) {
        s.value = o.value;
        s.dispatchEvent(new Event('input', {bubbles: true}));
        s.dispatchEvent(new Event('change', {bubbles: true}));
        if (window.jQuery) { window.jQuery(s).trigger('change'); }
        return o.text.trim();
      }
    }
  }
  return null;
}
"""


class BrowserSender:
    """يسجل الدخول مرة واحدة ثم يملأ نموذج الطلب لكل رابط."""

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.pw = None
        self.browser = None
        self.page = None
        self.logged_in = False

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=True, args=["--no-sandbox"]
        )
        context = self.browser.new_context(
            locale="ar", viewport={"width": 1280, "height": 1800}
        )
        context.set_default_timeout(30000)
        self.page = context.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.pw:
                self.pw.stop()

    # ---------- أدوات مساعدة ----------
    def _first_visible(self, *locators):
        for loc in locators:
            try:
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
        return None

    def _link_field(self):
        p = self.page
        return self._first_visible(
            p.locator("#orders-link"),
            p.get_by_label("الرابط", exact=True),
            p.locator("input[name*='link' i]"),
            p.locator("input[placeholder*='رابط']"),
        )

    def _quantity_field(self):
        p = self.page
        return self._first_visible(
            p.locator("#orders-quantity"),
            p.get_by_label("الكمية", exact=True),
            p.locator("input[name*='quantity' i]"),
        )

    def _debug(self, name):
        """لقطة شاشة عند الفشل (تُرفع كـ artifact في التشغيل اليدوي فقط)."""
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            self.page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}.png"), full_page=True)
            log(f"📸 حُفظت لقطة تشخيص: debug/{name}.png")
        except Exception as e:
            log(f"⚠️ تعذر حفظ لقطة التشخيص: {type(e).__name__}")

    # ---------- الدخول ----------
    def _login(self):
        p = self.page
        p.goto(SITE_URL + "/", wait_until="domcontentloaded")
        pw_field = p.locator("input[type='password']").first
        try:
            pw_field.wait_for(state="visible", timeout=20000)
        except Exception:
            # ربما نحن مسجلون دخولاً أصلاً
            return self._link_field() is not None

        form = pw_field.locator("xpath=ancestor::form[1]")
        user_field = form.locator(
            "input[type='text'], input[type='email'], input:not([type])"
        ).first
        user_field.fill(self.username)
        pw_field.fill(self.password)

        submit = self._first_visible(
            form.locator("button[type='submit'], input[type='submit']"),
            form.get_by_role("button", name="تسجيل الدخول"),
        )
        if submit is None:
            raise RuntimeError("زر تسجيل الدخول غير موجود")
        submit.click()
        p.wait_for_load_state("networkidle")

        still = p.locator("input[type='password']")
        if still.count() and still.first.is_visible():
            return False
        return True

    # ---------- صفحة الطلب ----------
    def _open_order_page(self):
        p = self.page
        if self._link_field():
            return True
        for path in ("/neworder", "/new-order", "/"):
            p.goto(SITE_URL + path, wait_until="domcontentloaded")
            p.wait_for_load_state("networkidle")
            if self._link_field():
                return True
        btn = p.get_by_text("طلب جديد", exact=True)
        if btn.count():
            btn.first.click()
            p.wait_for_load_state("networkidle")
        return self._link_field() is not None

    def _dismiss_popups(self):
        """إغلاق النوافذ الإعلانية الظاهرة التي قد تحجب نموذج الطلب."""
        for dialog in (self.page.locator("[role='dialog']"), self.page.locator(".modal")):
            for i in range(dialog.count()):
                item = dialog.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    close = item.locator("button[class*='close'], [aria-label*='close' i]").first
                    if close.count() and close.is_visible():
                        close.click(force=True)
                        self.page.wait_for_timeout(300)
                except Exception:
                    continue

    def _select(self, needle, retries=10):
        for _ in range(retries):
            chosen = self.page.evaluate(SELECT_JS, needle)
            if chosen:
                return chosen
            self.page.wait_for_timeout(500)
        return None

    # ---------- إرسال طلب ----------
    def __call__(self, link):
        """يرجع: 'ok' | 'fatal' | 'fail'."""
        p = self.page
        try:
            if not self.logged_in:
                if not self._login():
                    log("❌ فشل تسجيل الدخول: تحقق من اسم المستخدم وكلمة المرور.")
                    self._debug("login_failed")
                    return "fatal"
                self.logged_in = True
                log("🔓 تم تسجيل الدخول.")

            if not self._open_order_page():
                log("❌ لم أجد نموذج الطلب في الموقع.")
                self._debug("no_order_form")
                return "fail"

            self._dismiss_popups()
            if not self._select(CATEGORY_TEXT):
                log(f"❌ لم أجد القسم: {CATEGORY_TEXT}")
                self._debug("no_category")
                return "fail"
            p.wait_for_timeout(800)

            service = self._select(SERVICE_TEXT)
            if not service:
                log(f"❌ لم أجد الخدمة رقم {SERVICE_TEXT}")
                self._debug("no_service")
                return "fail"

            link_field = self._link_field()
            qty_field = self._quantity_field()
            if link_field is None or qty_field is None:
                log("❌ حقل الرابط أو الكمية غير موجود.")
                self._debug("no_fields")
                return "fail"
            link_field.fill(link)
            qty_field.fill(QUANTITY)

            confirm = self._first_visible(
                p.get_by_role("button", name=CONFIRM_TEXT),
                p.locator(f"button:has-text('{CONFIRM_TEXT}')"),
                p.locator(f"input[type='submit'][value*='{CONFIRM_TEXT}']"),
            )
            if confirm is None:
                log(f"❌ زر «{CONFIRM_TEXT}» غير موجود.")
                self._debug("no_confirm")
                return "fail"
            confirm.click()

            try:
                p.wait_for_function(
                    "(t) => document.body.innerText.includes(t)",
                    arg=SUCCESS_TEXT,
                    timeout=30000,
                )
            except Exception:
                alerts = p.locator(".alert, [role='alert']")
                msg = ""
                if alerts.count():
                    msg = alerts.first.inner_text().strip().replace("\n", " ")[:200]
                log(f"⚠️ لم تظهر رسالة النجاح. رسالة الموقع: {msg or 'لا توجد'}")
                self._debug("no_success")
                if "رصيد" in msg or "balance" in msg.lower():
                    return "fatal"
                return "fail"

            body = p.inner_text("body")
            m = re.search(r"الرقم\s*:?\s*(\d+)|(\d+)\s*:?\s*الرقم", body)
            order_id = (m.group(1) or m.group(2)) if m else "؟"
            log(f"✅ تم الطلب عبر الموقع. رقم الطلب: {order_id}")
            # العودة لصفحة جديدة نظيفة للطلب التالي في نفس التشغيل
            self.page.goto(SITE_URL + "/", wait_until="domcontentloaded")
            return "ok"

        except Exception as e:
            log(f"⚠️ خطأ أثناء التنفيذ: {type(e).__name__}: {str(e)[:150]}")
            self._debug("exception")
            return "fail"


def pending_links(attempts):
    done = set(read_lines(DONE_PATH))
    result = []
    for link in read_lines(LINKS_PATH):
        if link in done:
            continue
        if attempts.get(link, 0) >= MAX_ATTEMPTS_PER_LINK:
            continue
        if link not in result:
            result.append(link)
    return result


def run_send(sender):
    attempts = load_attempts()
    queue = pending_links(attempts)
    if not queue:
        log("💤 الطابور فارغ، لا شيء للتنفيذ.")
        return 0

    log(f"📋 في الطابور: {len(queue)} رابط.")
    tries = 0
    for link in queue:
        if tries >= MAX_TRIES_PER_RUN:
            break
        tries += 1
        log(f"معالجة: {link}")
        status = sender(link)

        if status == "ok":
            append_lines(DONE_PATH, [link])
            return 0  # رابط ناجح واحد لكل تشغيل
        if status == "fatal":
            log("⏹️ توقف: تحقق من المفتاح أو الرصيد.")
            return 1

        attempts[link] = attempts.get(link, 0) + 1
        save_attempts(attempts)
        if attempts[link] >= MAX_ATTEMPTS_PER_LINK:
            log(f"🚫 تم تخطي الرابط نهائياً بعد {MAX_ATTEMPTS_PER_LINK} محاولات فاشلة.")

    log("⚠️ لم ينجح أي طلب في هذا التشغيل.")
    return 0


def main():
    username = os.environ.get("SITE_USERNAME", "").strip()
    password = os.environ.get("SITE_PASSWORD", "")
    api_key = os.environ.get("API_KEY", "").strip()

    if username and password:
        mode = "browser"
    elif api_key:
        mode = "api"
    else:
        log("❌ لا توجد بيانات دخول. أضف SITE_USERNAME و SITE_PASSWORD (أو API_KEY) في Secrets.")
        return 1

    log(f"🚀 بدء التشغيل | الحساب: @{X_HANDLE} | الوضع: {mode}")

    try:
        pull_once()
    except Exception as e:
        log(f"❌ خطأ في السحب: {type(e).__name__}: {e}")

    # لا نفتح المتصفح إن لم يكن هناك ما يُنفَّذ
    if not pending_links(load_attempts()):
        log("💤 الطابور فارغ، لا شيء للتنفيذ.")
        return 0

    if mode == "api":
        return run_send(lambda link: send_order(api_key, link))

    with BrowserSender(username, password) as sender:
        return run_send(sender)


if __name__ == "__main__":
    sys.exit(main())
