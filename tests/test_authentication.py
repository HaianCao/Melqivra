"""Framework-neutral authentication adapter tests."""

from __future__ import annotations

import unittest

from agent_music.authentication import (
    AuthenticationError,
    DevelopmentHeaderAuthenticator,
    StaticBearerAuthenticator,
)
from agent_music.domain import UserId


class AuthenticationTests(unittest.TestCase):
    def test_development_header_is_explicit_and_validated(self) -> None:
        authenticator = DevelopmentHeaderAuthenticator()
        principal = authenticator.authenticate({"x-user-id": "usr_alice"})
        self.assertEqual(UserId("usr_alice"), principal.user_id)
        self.assertEqual("development_header", principal.authentication_method)
        for headers in ({}, {"x-user-id": "../alice"}):
            with self.subTest(headers=headers), self.assertRaises(AuthenticationError):
                authenticator.authenticate(headers)

    def test_bearer_tokens_are_injected_and_do_not_trust_identity_headers(self) -> None:
        token = "a-secure-test-token"
        authenticator = StaticBearerAuthenticator({token: UserId("usr_alice")})
        principal = authenticator.authenticate(
            {
                "authorization": f"Bearer {token}",
                "x-user-id": "usr_bob",
            }
        )
        self.assertEqual(UserId("usr_alice"), principal.user_id)
        self.assertEqual("bearer", principal.authentication_method)
        for authorization in ("", "Basic secret", "Bearer wrong-token-value"):
            with self.subTest(authorization=authorization), self.assertRaises(AuthenticationError):
                authenticator.authenticate({"authorization": authorization})

    def test_bearer_configuration_rejects_weak_or_invalid_entries(self) -> None:
        with self.assertRaises(ValueError):
            StaticBearerAuthenticator({})
        with self.assertRaises(ValueError):
            StaticBearerAuthenticator({"short": UserId("usr_alice")})
        with self.assertRaises(ValueError):
            StaticBearerAuthenticator({"valid-token-value": UserId("../alice")})


if __name__ == "__main__":
    unittest.main()
