"""Administrative CLI command to bootstrap initial tenant organization and owner account.

Usage:
    python -m app.cli.bootstrap --name "Acme Corp" --slug "acme" --email "owner@acme.com" [--full-name "Admin"]

Options:
    --name, --tenant-name       Organization display name (required)
    --slug, --tenant-slug       Unique organization slug (required)
    --email, --owner-email      Initial owner email address (required)
    --full-name, --owner-name   Initial owner full name (optional)
    --password-stdin            Read owner password from standard input (recommended for automation)
    --dry-run                   Perform preflight validation and database check without writing records
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Optional, Sequence

from sqlalchemy.exc import DatabaseError, OperationalError, SQLAlchemyError

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.persistence.database import get_db_context
from app.tenancy.service import bootstrap_tenant_and_owner

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.bootstrap",
        description="Bootstrap the initial tenant organisation and its first owner account.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--name",
        "--tenant-name",
        dest="name",
        required=True,
        help="Organization display name (e.g., 'Acme Corp').",
    )
    parser.add_argument(
        "--slug",
        "--tenant-slug",
        dest="slug",
        required=True,
        help="Unique URL/tenant slug (alphanumeric and hyphens, e.g., 'acme').",
    )
    parser.add_argument(
        "--email",
        "--owner-email",
        dest="email",
        required=True,
        help="Owner user email address.",
    )
    parser.add_argument(
        "--full-name",
        "--owner-name",
        dest="full_name",
        default=None,
        help="Owner user full name (optional).",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        default=False,
        help="Read password from standard input instead of prompting interactively.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preflight validation: verify inputs and database availability without persisting records.",
    )
    return parser


def read_password(args: argparse.Namespace) -> str:
    """Read password securely from stdin or interactive prompt. Never from CLI flags."""
    if args.password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise ValidationError("No password provided via stdin.")
        pw = line.rstrip("\r\n")
        if not pw:
            raise ValidationError("Password provided via stdin must not be blank.")
        return pw

    # Interactive hidden prompt
    if sys.stdin.isatty():
        try:
            pw = getpass.getpass("Enter initial owner password: ")
            if not pw:
                raise ValidationError("Password must not be blank.")
            pw_confirm = getpass.getpass("Confirm initial owner password: ")
            if pw != pw_confirm:
                raise ValidationError("Passwords do not match.")
            return pw
        except (EOFError, KeyboardInterrupt):
            raise ValidationError("Password entry cancelled.")

    if args.dry_run:
        # Non-interactive dry run without explicit --password-stdin
        return "PreflightValidationPassword123!"

    raise ValidationError("Non-interactive execution requires --password-stdin to supply the password.")


def execute_bootstrap(argv: Optional[Sequence[str]] = None) -> int:
    """Execute bootstrap command. Returns process exit code (0 for success, 1 for error)."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        password = read_password(args)
    except ValidationError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 1

    try:
        with get_db_context() as db:
            result = bootstrap_tenant_and_owner(
                db,
                name=args.name,
                slug=args.slug,
                email=args.email,
                password=password,
                full_name=args.full_name,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                db.rollback()
    except (ValidationError, ConflictError) as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 1
    except (OperationalError, DatabaseError, SQLAlchemyError) as err:
        logger.error("bootstrap.db_failure", error_type=type(err).__name__)
        print(
            "Error: Database connection failed. Verify CY_DATABASE_URL and that the database is reachable.",
            file=sys.stderr,
        )
        return 1
    except Exception as err:
        logger.error("bootstrap.unexpected_failure", error_type=type(err).__name__)
        print("Error: An unexpected error occurred during bootstrap.", file=sys.stderr)
        return 1

    if result.get("status") == "dry_run":
        print("\n[DRY-RUN] Preflight validation passed. No database changes were made.")
        print(f"  Organization: {result['tenant_name']} (slug: {result['tenant_slug']})")
        print(f"  Owner Email:  {result['owner_email']}")
        if result.get("owner_full_name"):
            print(f"  Owner Name:   {result['owner_full_name']}")
        print(f"  Role:         {result['role']}")
        return 0

    print("\n[SUCCESS] Organization and initial owner account successfully created.")
    print(f"  Tenant ID:    {result['tenant_id']}")
    print(f"  Tenant Name:  {result['tenant_name']}")
    print(f"  Tenant Slug:  {result['tenant_slug']}")
    print(f"  User ID:      {result['user_id']}")
    print(f"  Owner Email:  {result['owner_email']}")
    if result.get("owner_full_name"):
        print(f"  Owner Name:   {result['owner_full_name']}")
    print(f"  Role:         {result['role']}")
    print("\nThe owner may now log in via POST /v2/auth/token to obtain an access token.")
    return 0


def main() -> None:
    sys.exit(execute_bootstrap())


if __name__ == "__main__":
    main()
