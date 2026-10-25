import time

from django.core.management.base import BaseCommand

from notifications.redisdb import PushEndpoint
from notifications.utils.fcm import (
    DEAD_TOKEN_ERRORS,
    fcm_validate,
)
from notifications.utils.push_endpoints import unregister_endpoint

# Prefix of the client identifiers sent by the placement app
PHONE_PREFIX = 'placement-app:'

# A phone posts its token on every launch, so one that has not for this long
# is taken to be uninstalled
STALE_AFTER = 90 * 24 * 60 * 60

# Most tokens that FCM accepts in one multicast message
VALIDATE_BATCH_SIZE = 500


class Command(BaseCommand):
    """
    This class describes the command to be executed
    """

    help = """Delete the push tokens that will never receive a push again:
    placement app tokens not posted for 90 days, which also leave their
    topics, and any token that FCM reports as unregistered when sent a dry run
    message, which delivers nothing. Meant to be run weekly.
    Usage: django-admin prunepushendpoints [--dry-run]
    """

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        verb = 'Would delete' if dry_run else 'Deleted'
        now = int(time.time())

        endpoints = list()
        stale = 0
        for person_id in PushEndpoint.persons():
            seen = PushEndpoint.fetch_seen(person_id)
            for field, token in PushEndpoint.fetch_fields(person_id).items():
                # Every post records when it was seen, so a phone token
                # without a record is left alone rather than guessed at
                last_seen = seen.get(field)
                if (
                    field.startswith(PHONE_PREFIX)
                    and last_seen is not None
                    and now - last_seen > STALE_AFTER
                ):
                    stale += 1
                    self.stdout.write(
                        f'{verb} the stale token of person {person_id} '
                        f'under {field}'
                    )
                    if not dry_run:
                        unregister_endpoint(person_id, field)
                else:
                    endpoints.append((person_id, field, token))

        dead = unchecked = 0
        for i in range(0, len(endpoints), VALIDATE_BATCH_SIZE):
            batch = endpoints[i:i + VALIDATE_BATCH_SIZE]
            response = fcm_validate([token for _, _, token in batch])
            if response is None:
                unchecked += len(batch)
                continue

            for (person_id, field, token), result in zip(
                    batch, response.responses
            ):
                if isinstance(result.exception, DEAD_TOKEN_ERRORS):
                    dead += 1
                    self.stdout.write(
                        f'{verb} the dead token of person {person_id} '
                        f'under {field}'
                    )
                    if not dry_run:
                        PushEndpoint.remove(person_id, field, token)

        self.stdout.write(
            f'{verb} {stale} stale and {dead} dead tokens out of '
            f'{stale + len(endpoints)}'
        )
        if unchecked:
            self.stderr.write(
                f'{unchecked} tokens could not be checked with FCM'
            )
