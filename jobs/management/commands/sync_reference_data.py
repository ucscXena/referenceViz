"""
Sync reference-catalog tables between the 'default' (dev) and 'production'
databases.

Copies UCEModel, ReferenceGroup, Reference, and DocumentChunk.  Records
present in the destination but absent from the source are soft-deleted
(Reference is_active=False) when they have dependent Projections, or
hard-deleted when they do not.

Default direction is dev → prod.  Use --reverse to go prod → dev (e.g. for
the initial seed of a newly created dev database).

Run on the dev host (where both DB aliases are configured):
  python manage.py sync_reference_data                   # dev → prod
  python manage.py sync_reference_data --reverse         # prod → dev
  python manage.py sync_reference_data --dry-run
  python manage.py sync_reference_data --exclude-chunks
"""

from django.core.management.base import BaseCommand
from django.db import connections, transaction

from jobs.models import DocumentChunk, Reference, ReferenceGroup, UCEModel


class Command(BaseCommand):
    help = 'Sync reference-catalog tables between default (dev) and production DBs'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reverse', action='store_true',
            help='Sync prod → dev instead of the default dev → prod',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would change without writing anything',
        )
        parser.add_argument(
            '--exclude-chunks', action='store_true',
            help='Skip DocumentChunk sync (faster; chunks can be rebuilt with build_rag)',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        skip_chunks = options['exclude_chunks']

        src = 'production' if options['reverse'] else 'default'
        dst = 'default'    if options['reverse'] else 'production'

        if 'production' not in connections:
            self.stderr.write(
                "No 'production' database configured.  "
                "Add a 'production' entry to DATABASES in site_settings_private.py."
            )
            return

        self.stdout.write(f'Direction: {src} → {dst}')
        if dry:
            self.stdout.write('-- DRY RUN — no changes will be written --\n')

        self._sync_uce_models(src, dst, dry)
        self._sync_reference_groups(src, dst, dry)
        self._sync_references(src, dst, dry)
        self._fix_default_versions(src, dst, dry)
        if not options['reverse']:
            self._delete_orphans(src, dst, dry)
        if not skip_chunks:
            self._sync_chunks(src, dst, dry)

        self.stdout.write(self.style.SUCCESS('Done.'))

    # ── UCEModel ───────────────────────────────────────────────────────────────

    def _sync_uce_models(self, src, dst, dry):
        src_rows = list(UCEModel.objects.using(src).values())
        self.stdout.write(f'UCEModel: {len(src_rows)} in source')
        for row in src_rows:
            pk = row['id']
            defaults = {k: v for k, v in row.items() if k not in ('id', 'created_at')}
            try:
                obj = UCEModel.objects.using(dst).get(pk=pk)
                changed = [k for k, v in defaults.items() if getattr(obj, k) != v]
                if changed:
                    self.stdout.write(f'  UCEModel {obj.name!r}: update {changed}')
                    if not dry:
                        for k, v in defaults.items():
                            setattr(obj, k, v)
                        obj.save(using=dst, update_fields=list(defaults.keys()))
            except UCEModel.DoesNotExist:
                self.stdout.write(f'  UCEModel {row["name"]!r}: create (pk={pk})')
                if not dry:
                    UCEModel.objects.using(dst).create(**row)

    # ── ReferenceGroup ─────────────────────────────────────────────────────────

    def _sync_reference_groups(self, src, dst, dry):
        src_rows = list(ReferenceGroup.objects.using(src).values())
        self.stdout.write(f'ReferenceGroup: {len(src_rows)} in source')
        for row in src_rows:
            pk = row['id']
            try:
                obj = ReferenceGroup.objects.using(dst).get(pk=pk)
                if obj.title != row['title']:
                    self.stdout.write(f'  ReferenceGroup {pk}: update title')
                    if not dry:
                        obj.title = row['title']
                        obj.save(using=dst, update_fields=['title'])
            except ReferenceGroup.DoesNotExist:
                self.stdout.write(f'  ReferenceGroup {row["title"]!r}: create')
                if not dry:
                    ReferenceGroup(id=pk, title=row['title']).save(using=dst)

    # ── Reference ──────────────────────────────────────────────────────────────

    def _sync_references(self, src, dst, dry):
        src_rows = list(Reference.objects.using(src).values())
        self.stdout.write(f'Reference: {len(src_rows)} in source')
        skip = {'created_at'}
        for row in src_rows:
            pk = row['id']
            defaults = {k: v for k, v in row.items() if k not in skip and k != 'id'}
            try:
                obj = Reference.objects.using(dst).get(pk=pk)
                changed = [k for k, v in defaults.items() if getattr(obj, k) != v]
                if changed:
                    self.stdout.write(f'  Reference {pk!r}: update {changed}')
                    if not dry:
                        for k, v in defaults.items():
                            setattr(obj, k, v)
                        obj.save(using=dst, update_fields=list(defaults.keys()))
            except Reference.DoesNotExist:
                self.stdout.write(f'  Reference {pk!r}: create')
                if not dry:
                    ref = Reference(id=pk, **defaults)
                    ref.save(using=dst)

    # ── Fix default_version pointers ───────────────────────────────────────────

    def _fix_default_versions(self, src, dst, dry):
        for rg in ReferenceGroup.objects.using(src).all():
            if rg.default_version_id is None:
                continue
            try:
                dst_rg = ReferenceGroup.objects.using(dst).get(pk=rg.pk)
            except ReferenceGroup.DoesNotExist:
                continue
            if dst_rg.default_version_id != rg.default_version_id:
                self.stdout.write(
                    f'  ReferenceGroup {rg.pk}: set default_version={rg.default_version_id!r}'
                )
                if not dry:
                    dst_rg.default_version_id = rg.default_version_id
                    dst_rg.save(using=dst, update_fields=['default_version'])

    # ── Delete orphans from destination ────────────────────────────────────────

    def _delete_orphans(self, src, dst, dry):
        src_ref_ids = set(Reference.objects.using(src).values_list('id', flat=True))
        src_rg_ids  = set(ReferenceGroup.objects.using(src).values_list('id', flat=True))
        src_uce_ids = set(UCEModel.objects.using(src).values_list('id', flat=True))

        # References
        for ref in Reference.objects.using(dst).exclude(id__in=src_ref_ids):
            has_projections = ref.projections.using(dst).exists()
            if has_projections:
                self.stdout.write(
                    f'  Reference {ref.id!r}: has projections in {dst} — '
                    f'setting is_active=False instead of deleting'
                )
                if not dry and ref.is_active:
                    ref.is_active = False
                    ref.save(using=dst, update_fields=['is_active'])
            else:
                self.stdout.write(f'  Reference {ref.id!r}: delete (no projections)')
                if not dry:
                    ref.delete(using=dst)

        # ReferenceGroups with no remaining References
        for rg in ReferenceGroup.objects.using(dst).exclude(id__in=src_rg_ids):
            if rg.versions.using(dst).exists():
                self.stdout.write(
                    f'  ReferenceGroup {rg.id}: skipping delete — has references in {dst}'
                )
            else:
                self.stdout.write(f'  ReferenceGroup {rg.id}: delete')
                if not dry:
                    rg.delete(using=dst)

        # UCEModels with no remaining References
        for uce in UCEModel.objects.using(dst).exclude(id__in=src_uce_ids):
            if uce.references.using(dst).exists():
                self.stdout.write(
                    f'  UCEModel {uce.name!r}: skipping delete — has references in {dst}'
                )
            else:
                self.stdout.write(f'  UCEModel {uce.name!r}: delete')
                if not dry:
                    uce.delete(using=dst)

    # ── DocumentChunk ──────────────────────────────────────────────────────────

    def _sync_chunks(self, src, dst, dry):
        src_source_ids = set(
            DocumentChunk.objects.using(src).values_list('source_id', flat=True).distinct()
        )
        dst_source_ids = set(
            DocumentChunk.objects.using(dst).values_list('source_id', flat=True).distinct()
        )

        to_add    = src_source_ids - dst_source_ids
        to_remove = dst_source_ids - src_source_ids
        to_update = src_source_ids & dst_source_ids

        total_src = DocumentChunk.objects.using(src).count()
        self.stdout.write(f'DocumentChunk: {total_src} in source across {len(src_source_ids)} source_ids')

        for sid in sorted(to_remove):
            n = DocumentChunk.objects.using(dst).filter(source_id=sid).count()
            self.stdout.write(f'  DocumentChunk source_id={sid!r}: delete {n} chunks')
            if not dry:
                DocumentChunk.objects.using(dst).filter(source_id=sid).delete()

        for sid in sorted(to_add | to_update):
            src_chunks = list(DocumentChunk.objects.using(src).filter(source_id=sid).values())
            dst_count  = DocumentChunk.objects.using(dst).filter(source_id=sid).count()
            self.stdout.write(
                f'  DocumentChunk source_id={sid!r}: replace {dst_count} → {len(src_chunks)} chunks'
            )
            if not dry:
                with transaction.atomic(using=dst):
                    DocumentChunk.objects.using(dst).filter(source_id=sid).delete()
                    objs = [DocumentChunk(**{k: v for k, v in row.items() if k != 'id'})
                            for row in src_chunks]
                    DocumentChunk.objects.using(dst).bulk_create(objs, batch_size=500)
