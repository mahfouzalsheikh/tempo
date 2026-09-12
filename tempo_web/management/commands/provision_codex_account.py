from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from tempo.agent_accounts import provision
from tempo_web.models import AgentAccount


class Command(BaseCommand):
    help = (
        "Provision a new account from a private Codex ChatGPT auth file without printing secrets."
    )

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True)
        parser.add_argument("--revision", required=True, type=int)
        parser.add_argument("--auth-file", required=True)
        parser.add_argument("--operator", required=True)

    def handle(self, *args, **options):
        try:
            user = get_user_model().objects.get(username=options["operator"])
            account = provision(user, options["account"], options["revision"], options["auth_file"])
        except (
            ValueError,
            OSError,
            PermissionDenied,
            ValidationError,
            IntegrityError,
            AgentAccount.DoesNotExist,
            get_user_model().DoesNotExist,
        ) as exc:
            raise CommandError(
                "Provisioning failed. Check the staff operator, connection revision, private "
                "ChatGPT login file and credential store. No existing file is replaced."
            ) from exc
        self.stdout.write(f"Login stored for {account.pk}; runtime verification is pending.")
