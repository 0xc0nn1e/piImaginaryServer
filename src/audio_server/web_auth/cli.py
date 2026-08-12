"""Local administrator password reset command."""

from __future__ import annotations

import argparse
import getpass

from audio_server.core.config import get_settings
from audio_server.core.database import create_database
from audio_server.web_auth.service import WebAuthError, create_web_auth_service


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset the web administrator password and revoke all web sessions."
    )
    parser.add_argument("username", help="Administrator username")
    arguments = parser.parse_args()

    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm new password: ")
    if not secrets_match(password, confirmation):
        parser.error("password confirmation does not match")

    settings = get_settings()
    database = create_database(settings.database_url)
    service = create_web_auth_service(settings, database.session_factory)
    try:
        service.reset_password(username=arguments.username, new_password=password)
    except WebAuthError as error:
        parser.exit(1, f"Password reset failed: {error.safe_message}\n")
    finally:
        database.engine.dispose()
    print("Password updated and all web sessions revoked.")


def secrets_match(first: str, second: str) -> bool:
    import secrets

    return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))
