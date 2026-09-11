"""Config.secret credential resolution, and the prefix match that used to over-reach."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memcal.config import Config


class TestSecretLookup(unittest.TestCase):
    def cfg(self, **env):
        return Config(home=Path(tempfile.gettempdir()), env=dict(env))

    def test_an_exact_name_matches(self):
        self.assertEqual(self.cfg(SLACK_TOKEN="xoxp").secret("SLACK_TOKEN"), "xoxp")

    def test_a_family_alias_matches_a_short_env_key(self):
        self.assertEqual(self.cfg(slack="xoxp").secret("SLACK_TOKEN", "slack"), "xoxp")

    def test_case_and_separators_do_not_matter(self):
        self.assertEqual(
            self.cfg(groupme_access_token="t").secret("GROUPME_ACCESS_TOKEN"), "t")

    def test_a_verbose_env_key_still_answers_via_the_specific_name(self):
        # A user who suffixes the canonical name still resolves.
        self.assertEqual(
            self.cfg(SLACK_TOKEN_PROD="xoxp").secret("SLACK_TOKEN", "slack"), "xoxp")

    def test_a_sibling_credential_never_answers_for_the_token(self):
        # The bug: the "slack" family alias prefix-matched SLACK_USER_ID, so a token
        # lookup came back with the user id. Only the specific name may prefix-match.
        cfg = self.cfg(SLACK_USER_ID="U123")
        self.assertIsNone(cfg.secret("SLACK_TOKEN", "slack"))

    def test_the_real_token_still_wins_when_a_sibling_is_also_present(self):
        cfg = self.cfg(SLACK_USER_ID="U123", SLACK_TOKEN="xoxp")
        self.assertEqual(cfg.secret("SLACK_TOKEN", "slack"), "xoxp")

    def test_two_specific_siblings_do_not_cross(self):
        cfg = self.cfg(TELEGRAM_API_ID="111", TELEGRAM_API_HASH="abc")
        self.assertEqual(cfg.secret("TELEGRAM_API_ID", "telegramapiid"), "111")
        self.assertEqual(cfg.secret("TELEGRAM_API_HASH", "telegramapihash"), "abc")

    def test_a_short_env_key_never_satisfies_a_longer_lookup(self):
        # `_` normalizes to "", so an empty prefix must never match everything.
        self.assertIsNone(self.cfg(_="x").secret("SLACK_TOKEN", "slack"))

    def test_a_missing_credential_is_none(self):
        self.assertIsNone(self.cfg().secret("SLACK_TOKEN", "slack"))


if __name__ == "__main__":
    unittest.main()
