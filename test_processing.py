import os
import json
import tempfile
import unittest

from account_rotation import select_account
from main import X_HANDLES, pull_once, run_send
from processing_state import load_failures, load_successes
from retry_link import is_allowed_link


class FakeSender:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, link):
        self.calls.append(link)
        return self.outcomes.pop(0) if self.outcomes else "ok"


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = self.temp.name
        self.links = [f"https://x.com/aljanoubvoice2/status/{number}" for number in range(1, 8)]
        with open(os.path.join(self.state, "telegram_links.txt"), "w", encoding="utf-8") as file:
            file.write("\n".join(self.links) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_run_limit_and_success_count_carry_over_to_five(self):
        first = FakeSender(["ok"] * 5)
        self.assertEqual(run_send(first, self.state), 0)
        self.assertEqual(len(first.calls), 5)
        self.assertEqual(load_successes(self.state)[self.links[0]], 1)
        second = FakeSender(["ok"] * 5)
        self.assertEqual(run_send(second, self.state), 0)
        self.assertEqual(len(second.calls), 5)
        self.assertEqual(load_successes(self.state)[self.links[0]], 2)
        for _ in range(3):
            run_send(FakeSender(["ok"] * 5), self.state)
        self.assertEqual(load_successes(self.state)[self.links[0]], 5)
        later = FakeSender(["ok"] * 5)
        run_send(later, self.state)
        self.assertEqual(later.calls, self.links[5:])

    def test_failure_does_not_increase_success_and_three_failures_skip(self):
        run_send(FakeSender(["fail"] * 5), self.state)
        self.assertEqual(load_successes(self.state), {})
        self.assertEqual(load_failures(self.state)[self.links[0]], 1)
        run_send(FakeSender(["fail"] * 5), self.state)
        run_send(FakeSender(["fail"] * 5), self.state)
        self.assertEqual(load_failures(self.state)[self.links[0]], 3)
        later = FakeSender(["ok"] * 5)
        run_send(later, self.state)
        self.assertNotIn(self.links[0], later.calls)

    def test_legacy_done_links_stay_complete_and_unchanged(self):
        done_path = os.path.join(self.state, "done_links.txt")
        with open(done_path, "w", encoding="utf-8") as file:
            file.write(self.links[0] + "\n")
        with open(done_path, "rb") as file:
            original = file.read()
        sender = FakeSender(["ok"] * 5)
        run_send(sender, self.state)
        self.assertNotIn(self.links[0], sender.calls)
        self.assertNotIn(self.links[0], load_successes(self.state))
        with open(done_path, "rb") as file:
            self.assertEqual(file.read(), original)

    def test_failure_only_updates_failure_counter(self):
        run_send(FakeSender(["fail"] * 5), self.state)
        self.assertEqual(load_successes(self.state), {})
        self.assertEqual(load_failures(self.state)[self.links[0]], 1)

    def test_account_rotation_is_shared_and_persists_1_2_3_1(self):
        env = {
            "SITE_USERNAME_1": "user-1", "SITE_PASSWORD_1": "pass-1",
            "SITE_USERNAME_2": "user-2", "SITE_PASSWORD_2": "pass-2",
            "SITE_USERNAME_3": "user-3", "SITE_PASSWORD_3": "pass-3",
        }
        selected = [select_account(self.state, env) for _ in range(4)]
        self.assertEqual([item[0] for item in selected], [1, 2, 3, 1])
        self.assertEqual(selected[0][1:], ("user-1", "pass-1"))
        with open(os.path.join(self.state, "account_rotation.json"), encoding="utf-8") as file:
            self.assertEqual(json.load(file), {"next_account": 2})

    def test_missing_secret_does_not_advance_or_expose_value(self):
        env = {"SITE_USERNAME_1": "user-1", "SITE_PASSWORD_1": ""}
        with self.assertRaisesRegex(RuntimeError, "الحساب 1") as context:
            select_account(self.state, env)
        self.assertNotIn("user-1", str(context.exception))
        self.assertFalse(os.path.exists(os.path.join(self.state, "account_rotation.json")))

    def test_corrupt_rotation_state_recovers_at_account_one(self):
        path = os.path.join(self.state, "account_rotation.json")
        with open(path, "w", encoding="utf-8") as file:
            file.write("not-json")
        account, username, password = select_account(
            self.state,
            {"SITE_USERNAME_1": "user-1", "SITE_PASSWORD_1": "pass-1"},
        )
        self.assertEqual((account, username, password), (1, "user-1", "pass-1"))
        with open(path, encoding="utf-8") as file:
            self.assertEqual(json.load(file), {"next_account": 2})

    def test_initial_poll_keeps_latest_post_per_source_account(self):
        import main
        discovered = [
            f"https://x.com/{handle}/status/{number}"
            for handle, number in ((X_HANDLES[0], 100), (X_HANDLES[0], 200),
                                   (X_HANDLES[1], 150), (X_HANDLES[2], 175))
        ]
        original_fetch = main.fetch_all_links
        original_processed = main.PROCESSED_PATH
        original_links = main.LINKS_PATH
        try:
            main.fetch_all_links = lambda: discovered
            main.PROCESSED_PATH = os.path.join(self.state, "processed_links.txt")
            main.LINKS_PATH = os.path.join(self.state, "telegram_links.txt")
            # Ensure this is a clean first run, independent of setUp's send queue.
            os.remove(main.LINKS_PATH)
            selected = pull_once()
            self.assertEqual(selected, [
                f"https://x.com/{X_HANDLES[0]}/status/200",
                f"https://x.com/{X_HANDLES[1]}/status/150",
                f"https://x.com/{X_HANDLES[2]}/status/175",
            ])
            with open(main.PROCESSED_PATH, encoding="utf-8") as file:
                self.assertEqual(set(file.read().splitlines()), set(discovered))
        finally:
            main.fetch_all_links = original_fetch
            main.PROCESSED_PATH = original_processed
            main.LINKS_PATH = original_links

    def test_retry_accepts_only_configured_source_accounts(self):
        for handle in X_HANDLES:
            self.assertTrue(is_allowed_link(f"https://x.com/{handle}/status/123"))
        self.assertFalse(is_allowed_link("https://x.com/other_account/status/123"))


if __name__ == "__main__":
    unittest.main()
