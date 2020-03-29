"""Database models used by django-reversion."""

from __future__ import unicode_literals

try:
    from django.contrib.contenttypes.fields import GenericForeignKey
except ImportError:  # Django < 1.9 pragma: no cover
    from django.contrib.contenttypes.generic import GenericForeignKey

from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, IntegrityError, transaction
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import force_text, python_2_unicode_compatible
from django.utils.safestring import mark_safe
from django.utils.html import format_html_join
from django.template.defaultfilters import truncatechars

from chamber.utils.datastructures import ChoicesNumEnum

from reversion.filters import (
    VersionIDFilter, VersionContextTypeFilter, RelatedObjectsFilter, RelatedObjectsWithIntIdFilter
)
from reversion import config


def safe_revert(versions):
    """
    Attempts to revert the given models contained in the give versions.

    This method will attempt to resolve dependencies between the versions to revert
    them in the correct order to avoid database integrity errors.
    """
    unreverted_versions = []
    for version in versions:
        try:
            with transaction.atomic():
                version.revert()
        except (IntegrityError, ObjectDoesNotExist):  # pragma: no cover
            unreverted_versions.append(version)
    if len(unreverted_versions) == len(versions):  # pragma: no cover
        raise RevertError('Could not revert revision, due to database integrity errors.')
    if unreverted_versions:  # pragma: no cover
        safe_revert(unreverted_versions)


class RevertError(Exception):

    """Exception thrown when something goes wrong with reverting a model."""


UserModel = getattr(settings, 'AUTH_USER_MODEL', 'auth.User')


@python_2_unicode_compatible
class Revision(models.Model):

    """A group of related object versions."""

    manager_slug = models.CharField(max_length=191, db_index=True, default='default')
    created_at = models.DateTimeField(verbose_name=_('created at'), auto_now_add=True, db_index=True,
                                      help_text=_('The date and time this revision was created.'))
    user = models.ForeignKey(UserModel, verbose_name=_('user'), blank=True, null=True, on_delete=models.SET_NULL,
                             help_text=_('The user who created this revision.'))
    comment = models.TextField(verbose_name=_('comment'), blank=True, help_text=_('A text comment on this revision.'))

    def revert(self, delete=False):
        """Reverts all objects in this revision."""
        version_set = self.versions.filter(type__in=(Version.TYPE.CREATED, Version.TYPE.CHANGED, Version.TYPE.FOLLOW))
        # Optionally delete objects no longer in the current revision.
        if delete:
            # Get a dict of all objects in this revision.
            old_revision = set()
            for version in version_set:
                try:
                    obj = version.object
                except ContentType.objects.get_for_id(version.content_type_id).model_class().DoesNotExist:
                    pass
                else:
                    old_revision.add(obj)
            # Calculate the set of all objects that are in the revision now.
            from reversion.revisions import RevisionManager
            current_revision = RevisionManager.get_manager(self.manager_slug)._follow_relationships(
                (obj for obj in old_revision if obj is not None), False)
            # Delete objects that are no longer in the current revision.
            for item in current_revision:
                if item not in old_revision:
                    item.delete()
        # Attempt to revert all revisions.
        safe_revert(version_set)

    def __str__(self):
        """Returns a unicode representation."""
        return '#%s' % self.pk

    class Meta:
        app_label = 'reversion'
        ordering = ('-created_at',)
        verbose_name = _('data revision')
        verbose_name_plural = _('data revisions')


def has_int_pk(model):
    """Tests whether the given model has an integer primary key."""
    pk = model._meta.pk
    return (
        (
            isinstance(pk, (models.IntegerField, models.AutoField)) and
            not isinstance(pk, models.BigIntegerField)
        ) or (
            isinstance(pk, models.ForeignKey) and has_int_pk(pk.rel.to)
        )
    )


@python_2_unicode_compatible
class Version(models.Model):

    """A saved version of a database model."""

    TYPE = ChoicesNumEnum(
        ('CREATED', _('Created'), 1),
        ('CHANGED', _('Changed'), 2),
        ('DELETED', _('Deleted'), 3),
        ('FOLLOW', _('Follow'), 4),
        ('AUDIT', _('Audit'), 5),
    )

    revision = models.ForeignKey(Revision, verbose_name=_('revision'),
                                 help_text=_('The revision that contains this version.'), related_name='versions')
    object_id = models.TextField(verbose_name=_('object id'),
                                 help_text=_('Primary key of the model under version control.'))
    object_id_int = models.IntegerField(
        verbose_name=_('object id int'), blank=True, null=True, db_index=True,
        help_text=_('An indexed, integer version of the stored model\'s primary key, used for faster lookups.'),
    )
    content_type = models.ForeignKey(ContentType, help_text=_('Content type of the model under version control.'))

    # A link to the current instance, not the version stored in this Version!
    object = GenericForeignKey()

    format = models.CharField(verbose_name=_('format'), max_length=255,
                              help_text=_('The serialization format used by this model.'))
    serialized_data = models.TextField(verbose_name=_('serialized data'),
                                       help_text=_('The serialized form of this version of the model.'))
    object_repr = models.TextField(verbose_name=_('object representation'),
                                   help_text=_('A string representation of the object.'))
    type = models.PositiveIntegerField(verbose_name=_('version type'), choices=TYPE.choices)

    @property
    def object_version(self):
        """The stored version of the model."""
        data = self.serialized_data
        data = force_text(data.encode('utf8'))
        return list(serializers.deserialize(self.format, data, ignorenonexistent=True))[0]

    @property
    def flat_field_dict(self):
        object_version = self.object_version
        obj = object_version.object
        result = {}
        for field in obj._meta.fields:
            result[field.name] = field.value_from_object(obj)
        result.update(object_version.m2m_data)
        return result

    @property
    def field_dict(self):
        """
        A dictionary mapping field names to field values in this version
        of the model.

        This method will follow parent links, if present.
        """
        if not hasattr(self, '_field_dict_cache'):
            object_version = self.object_version
            obj = object_version.object
            result = {}
            for field in obj._meta.fields:
                result[field.name] = field.value_from_object(obj)
            result.update(object_version.m2m_data)
            # Add parent data.
            for parent_class, field in obj._meta.concrete_model._meta.parents.items():
                if obj._meta.proxy and parent_class == obj._meta.concrete_model:
                    continue
                content_type = ContentType.objects.get_for_model(parent_class)
                if field:
                    parent_id = force_text(getattr(obj, field.attname))
                else:
                    parent_id = obj.pk
                try:
                    parent_version = Version.objects.get(revision__id=self.revision_id,
                                                         content_type=content_type,
                                                         object_id=parent_id)
                except Version.DoesNotExist:  # pragma: no cover
                    pass
                else:
                    result.update(parent_version.field_dict)
            setattr(self, '_field_dict_cache', result)
        return getattr(self, '_field_dict_cache')

    def revert(self):
        """Recovers the model in this version."""
        self.object_version.save()

    @cached_property
    def cached_instances(self):
        """
        Return and cache instance with its parents
        """

        obj = self.object_version.object
        result = [obj]
        for parent_class in obj._meta.get_parent_list():
            content_type = ContentType.objects.get_for_model(parent_class)
            parent_id = obj.pk
            try:
                parent_version = Version.objects.get(revision__id=self.revision_id,
                                                     content_type=content_type,
                                                     object_id=parent_id)
            except Version.DoesNotExist:
                pass
            else:
                result.append(parent_version.object_version.object)
        return result

    def reversion_editor(self):
        if self.revision.user:
            return self.revision.user.email

    def __getattr__(self, attr):
        # If child inst has attribute it only means that this attribute exists, but can be None and only set in parent
        if hasattr(self.cached_instances[0], attr):
            val = None
            for inst in self.cached_instances:
                val = getattr(inst, attr, None)
                if val is not None:
                    break
            return val
        else:
            raise AttributeError("%r object has no attribute %r" % (self.__class__, attr))

    def __str__(self):
        """Returns a unicode representation."""
        return self.object_repr

    class Meta:
        app_label = 'reversion'
        verbose_name = _('data version')
        verbose_name_plural = _('data versions')


@python_2_unicode_compatible
class AuditLog(models.Model):
    created_at = models.DateTimeField(verbose_name=_('created at'), auto_now_add=True, db_index=True)
    versions = models.ManyToManyField(Version, verbose_name=_('versions'))
    comment = models.TextField(verbose_name=_('comment'), blank=True, help_text=_('A text comment on this revision.'))
    priority = models.PositiveIntegerField(verbose_name=_('priority'), null=True, blank=True)
    slug = models.SlugField(verbose_name=_('slug'), null=True, blank=True)

    def short_comment(self):
        return truncatechars(self.comment, config.AUDIT_LOG_SHORT_COMMENT_LENGTH)
    short_comment.short_description = _('Comment')
    short_comment.filter_by = 'comment'
    short_comment.order_by = 'comment'

    def _related_objects(self, request):
        from is_core.utils import render_model_object_with_link

        rendered_objects = []
        for version in self.versions.all():
            obj = version.object
            if obj:
                rendered_objects.append((obj._meta.verbose_name, render_model_object_with_link(request, obj)))

        return mark_safe(', '.join(('{}: {}'.format(name, link) for name, link in rendered_objects)))

    def related_objects(self, request):
        return self._related_objects(request)
    related_objects.short_description = _('related objects')
    related_objects.filter = RelatedObjectsFilter

    def related_objects_with_int_id(self, request):
        return self._related_objects(request)
    related_objects_with_int_id.short_description = _('related objects')
    related_objects_with_int_id.filter = RelatedObjectsWithIntIdFilter

    def _related_objects_display(self, request):
        from is_core.utils import render_model_object_with_link

        rendered_objects = []
        for version in self.versions.all():
            obj = version.object
            if obj:
                rendered_objects.append((obj._meta.verbose_name, render_model_object_with_link(request, obj)))

        return mark_safe('<ul>{}</ul>'.format(
            format_html_join(
                '\n', '<li>{}: {}</li>',
                ((name, mark_safe(link)) for name, link in rendered_objects)
            )
        ))

    def related_objects_display(self, request):
        return self._related_objects_display(request)
    related_objects_display.short_description = _('related objects')
    related_objects_display.filter = RelatedObjectsFilter

    def related_objects_with_int_id_display(self, request):
        return self._related_objects_display(request)
    related_objects_with_int_id_display.short_description = _('related objects')
    related_objects_with_int_id_display.filter = RelatedObjectsWithIntIdFilter

    def revisions_display(self, request):
        from is_core.utils import render_model_object_with_link

        rendered_objects = []
        for revision in Revision.objects.filter(versions__in=self.versions.all()).distinct():
            rendered_objects.append(render_model_object_with_link(request, revision))
        return mark_safe(', '.join(rendered_objects))
    revisions_display.short_description = _('data revision')

    def content_types(self):
        return ', '.join([
            force_text(content_type)
            for content_type in ContentType.objects.filter(pk__in=self.versions.all().values('content_type').distinct())
        ])
    content_types.short_description = _('related content types')
    content_types.filter = VersionContextTypeFilter

    def object_pks(self):
        return ', '.join(self.versions.all().values_list('object_id', flat=True).distinct())
    object_pks.short_description = _('related object pks')
    object_pks.filter = VersionIDFilter

    def __str__(self):
        """Returns a unicode representation."""
        return '#{}'.format(self.pk)

    class Meta:
        app_label = 'reversion'
        verbose_name = _('audit log')
        verbose_name_plural = _('audit logs')
        ordering = ('-created_at',)
