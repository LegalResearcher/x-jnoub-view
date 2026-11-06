#!/usr/bin/env python3
"""إعادة محاولة رابط X واحد مع استخدام حالة المعالجة نفسها للنشر المجدول."""
from __future__ import annotations
import os
import re
import sys
from account_rotation import select_account
from main import BrowserSender, X_HANDLES, send_order
from processing_state import MAX_SUCCESSES_PER_LINK, can_process, load_failures, load_successes, record_result

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
LINK_PATTERN = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([^/]+)/status/(\d+)/?$", re.I)

def is_allowed_link(link: str) -> bool:
    match = LINK_PATTERN.fullmatch(link)
    return bool(match and match.group(1).lower() in {handle.lower() for handle in X_HANDLES})

def retry_one_link(link: str, sender) -> str:
    link = link.strip()
    if not is_allowed_link(link):
        allowed = "، ".join("@" + handle for handle in X_HANDLES)
        raise ValueError(f"الرابط يجب أن يكون منشوراً من أحد حسابات X: {allowed}")
    successes = load_successes(STATE_DIR)
    failures = load_failures(STATE_DIR)
    if not can_process(link, STATE_DIR, successes, failures):
        if successes.get(link, 0) >= MAX_SUCCESSES_PER_LINK:
            return "success_limit"
        return "not_eligible"
    status = sender(link)
    if status not in ("ok", "success"):
        record_result(link, "failure", STATE_DIR, successes, failures)
        return "failure"
    accepted, result = record_result(link, "ok", STATE_DIR, successes, failures)
    return result if accepted else result

def main() -> int:
    link = os.environ.get("TARGET_LINK", "").strip()
    if not is_allowed_link(link):
        allowed = "، ".join("@" + handle for handle in X_HANDLES)
        print(f"❌ رابط غير صالح؛ يجب أن يكون من أحد الحسابات: {allowed}", file=sys.stderr)
        return 2
    api_key = os.environ.get("API_KEY", "").strip()
    try:
        _, username, password = select_account(STATE_DIR)
        with BrowserSender(username, password) as sender:
            result = retry_one_link(link, sender)
    except RuntimeError as error:
        if not api_key:
            print(f"❌ لا توجد بيانات دخول للحساب أو API_KEY: {error}", file=sys.stderr)
            return 1
        result = retry_one_link(link, lambda value: send_order(api_key, value))
    print(f"نتيجة إعادة المحاولة: {result}")
    return 0 if result in ("success", "ok") else 1

if __name__ == "__main__":
    raise SystemExit(main())
