"""
Copy a single user account from the 'production' database to 'default' (dev).

Copies the User row, allauth EmailAddress records, and any SocialAccount +
SocialToken records.  Matches by email address — does not rely on integer PKs,
which may differ between the two databases.

Run on the dev host (where both DB aliases are configured):
  python manage.py copy_user user@example.com
  python manage.py copy_user user@example.com --dry-run
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

SRC = 'production'
DST = 'default'

User = get_user_model()


class Command(BaseCommand):
    help = 'Copy a user account from production DB to default (dev) DB'

    def add_arguments(self, parser):
        parser.add_argument('email', help='Email address of the user to copy')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would change without writing anything',
        )

    def handle(self, *args, **options):
        email = options['email'].strip().lower()
        dry   = options['dry_run']

        if SRC not in connections:
            raise CommandError(
                f"No '{SRC}' database configured.  "
                "Add a 'production' entry to DATABASES in site_settings_private.py."
            )

        if dry:
            self.stdout.write('-- DRY RUN — no changes will be written --\n')

        # ── Fetch user from production ─────────────────────────────────────────

        try:
            src_user = User.objects.using(SRC).get(email__iexact=email)
        except User.DoesNotExist:
            raise CommandError(f'No user with email {email!r} found in production.')

        self.stdout.write(
            f'Source user: {src_user.email} (pk={src_user.pk}, '
            f'username={src_user.username!r}, active={src_user.is_active})'
        )

        # ── Create or update user in dev ───────────────────────────────────────

        try:
            dst_user = User.objects.using(DST).get(email__iexact=email)
            self.stdout.write(f'  User: update existing (pk={dst_user.pk})')
            if not dry:
                dst_user.username    = src_user.username
                dst_user.first_name  = src_user.first_name
                dst_user.last_name   = src_user.last_name
                dst_user.is_active   = src_user.is_active
                dst_user.is_staff    = src_user.is_staff
                dst_user.is_superuser = src_user.is_superuser
                dst_user.password    = src_user.password
                dst_user.date_joined = src_user.date_joined
                dst_user.last_login  = src_user.last_login
                dst_user.save(using=DST)
        except User.DoesNotExist:
            self.stdout.write(f'  User: create new')
            dst_user = src_user  # will save with a new pk assigned by DST
            if not dry:
                dst_user.pk = None
                dst_user.save(using=DST)

        if dry:
            # Can't copy related records without a real dst_user.pk
            self.stdout.write('  (skipping related records in dry-run mode)')
            return

        # ── EmailAddress ───────────────────────────────────────────────────────

        try:
            from allauth.account.models import EmailAddress
            _sync_email_addresses(src_user, dst_user, dry, self.stdout)
        except ImportError:
            pass

        # ── SocialAccount + SocialToken ────────────────────────────────────────

        try:
            from allauth.socialaccount.models import SocialAccount, SocialToken
            _sync_social_accounts(src_user, dst_user, dry, self.stdout)
        except ImportError:
            pass

        self.stdout.write(self.style.SUCCESS(f'Done: {email}'))


def _sync_email_addresses(src_user, dst_user, dry, out):
    from allauth.account.models import EmailAddress

    src_addrs = list(EmailAddress.objects.using(SRC).filter(user=src_user).values())
    for row in src_addrs:
        email = row['email']
        try:
            dst_obj = EmailAddress.objects.using(DST).get(user=dst_user, email__iexact=email)
            changed = dst_obj.verified != row['verified'] or dst_obj.primary != row['primary']
            if changed:
                out.write(f'  EmailAddress {email!r}: update')
                if not dry:
                    dst_obj.verified = row['verified']
                    dst_obj.primary  = row['primary']
                    dst_obj.save(using=DST, update_fields=['verified', 'primary'])
        except EmailAddress.DoesNotExist:
            out.write(f'  EmailAddress {email!r}: create')
            if not dry:
                EmailAddress.objects.using(DST).create(
                    user=dst_user,
                    email=email,
                    verified=row['verified'],
                    primary=row['primary'],
                )


def _sync_social_accounts(src_user, dst_user, dry, out):
    from allauth.socialaccount.models import SocialAccount, SocialToken

    src_accounts = list(SocialAccount.objects.using(SRC).filter(user=src_user))
    for src_acc in src_accounts:
        try:
            dst_acc = SocialAccount.objects.using(DST).get(
                user=dst_user, provider=src_acc.provider, uid=src_acc.uid
            )
            out.write(f'  SocialAccount {src_acc.provider}/{src_acc.uid}: update extra_data')
            if not dry:
                dst_acc.extra_data = src_acc.extra_data
                dst_acc.last_login = src_acc.last_login
                dst_acc.save(using=DST, update_fields=['extra_data', 'last_login'])
        except SocialAccount.DoesNotExist:
            out.write(f'  SocialAccount {src_acc.provider}/{src_acc.uid}: create')
            if not dry:
                dst_acc = SocialAccount.objects.using(DST).create(
                    user=dst_user,
                    provider=src_acc.provider,
                    uid=src_acc.uid,
                    extra_data=src_acc.extra_data,
                    last_login=src_acc.last_login,
                    date_joined=src_acc.date_joined,
                )

        if dry:
            continue

        # Tokens are keyed by (account, app).  Copy the latest token for each app.
        for src_tok in SocialToken.objects.using(SRC).filter(account=src_acc):
            try:
                dst_tok = SocialToken.objects.using(DST).get(
                    account=dst_acc, app_id=src_tok.app_id
                )
                out.write(f'  SocialToken app_id={src_tok.app_id}: update')
                if not dry:
                    dst_tok.token         = src_tok.token
                    dst_tok.token_secret  = src_tok.token_secret
                    dst_tok.expires_at    = src_tok.expires_at
                    dst_tok.save(using=DST, update_fields=['token', 'token_secret', 'expires_at'])
            except SocialToken.DoesNotExist:
                out.write(f'  SocialToken app_id={src_tok.app_id}: create')
                if not dry:
                    SocialToken.objects.using(DST).create(
                        account=dst_acc,
                        app_id=src_tok.app_id,
                        token=src_tok.token,
                        token_secret=src_tok.token_secret,
                        expires_at=src_tok.expires_at,
                    )
