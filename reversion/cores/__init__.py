from django.utils.translation import ugettext_lazy as _

from is_core.main import UIRESTModelISCore

from reversion.models import Revision, AuditLog

from .views import ReversionDetailView, ReversionHistoryView, VersionInlineFormView


class DataRevisionISCore(UIRESTModelISCore):

    abstract = True
    model = Revision
    ui_list_fields = ('created_at', 'user', 'comment')
    rest_list_fields = ('pk',)
    rest_list_obj_fields = ('pk',)
    menu_group = 'data-revision'

    form_fieldsets = (
        (None, {'fields': ('created_at', 'user', 'comment')}),
        (_('Versions'), {'inline_view': VersionInlineFormView}),
    )
    form_readonly_fields = ('user', 'comment', 'serialized_data')

    create_permission = False
    delete_permission = False
    update_permission = False


class ReversionUIRESTModelISCore(UIRESTModelISCore):
    abstract = True
    create_permission = False
    delete_permission = False

    ui_detail_view = ReversionDetailView

    def get_view_classes(self):
        view_classes = super().get_view_classes()
        view_classes['history'] = (r'^/(?P<pk>\d+)/history/?$', ReversionHistoryView)
        return view_classes


class AuditLogUIRESTModelISCore(UIRESTModelISCore):

    abstract = True
    model = AuditLog
    ui_list_fields = ('created_at', 'related_objects', 'content_types', 'object_pks', 'short_comment', 'priority', 'slug')
    form_fields = ('created_at', 'comment', 'priority', 'slug', 'related_objects_display', 'revisions_display')
    menu_group = 'audit-log'
    create_permission = False
    delete_permission = False
    update_permission = False
    rest_extra_filter_fields = ('related_objects_with_int_id', 'related_objects_display', 'related_objects')
