from django import forms
from django.contrib import admin
from django.contrib.admin import ModelAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.core.exceptions import PermissionDenied
from django.db import transaction

from skeleton.accounts.models import User, UserProfile
from skeleton.accounts.services import create_identity, create_organization, deactivate_user
from skeleton.access.services import assert_user_deactivation
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.models import Organization
from skeleton.system.models import TableDefinition, FieldDefinition
from skeleton.audit.models import AuditEvent




class PlatformUserForm(forms.ModelForm):
    password = ReadOnlyPasswordHashField(disabled=True)
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['expected_revision'].initial = self.instance.revision

    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name', 'is_active', 'is_staff', 'is_superuser', 'password']


class PlatformUserCreateForm(forms.ModelForm):
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)
    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name']

    def clean(self):
        data = super().clean()
        if data.get('password1') != data.get('password2'):
            self.add_error('password2', 'Passwords do not match.')
        from django.contrib.auth.password_validation import validate_password
        if data.get('password1'):
            validate_password(data['password1'], self.instance)
        return data


class PlatformUserAdmin(ModelAdmin):
    form = PlatformUserForm
    list_display = ['email', 'is_active', 'is_staff', 'is_superuser', 'sysid']
    readonly_fields = ['sysid', 'auth_version', 'local_password_allowed', 'created_at', 'updated_at', 'revision', 'deleted_at']
    actions = None

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs['form'] = PlatformUserCreateForm
            kwargs['fields'] = ['email', 'first_name', 'last_name', 'password1', 'password2']
        return super().get_form(request, obj, **kwargs)

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            if not change:
                created = create_identity(email=obj.email, password=form.cleaned_data['password1'], first_name=obj.first_name, last_name=obj.last_name)
                obj.__dict__.update(created.__dict__)
                return
            previous = User.all_objects.select_for_update().get(pk=obj.pk)
            if form.cleaned_data['expected_revision'] != previous.revision:
                raise ConcurrentUpdate('The platform form is stale.')
            if obj.email != previous.email:
                raise PermissionDenied('Email changes require allauth verified email flow.')
            if previous.is_active and not obj.is_active:
                assert_user_deactivation(previous)
            fields = {'first_name', 'last_name', 'is_active', 'is_staff', 'is_superuser'}
            changed = {key for key in fields if getattr(previous, key) != getattr(obj, key)}
            if changed & {'is_active', 'is_staff', 'is_superuser'}:
                previous.auth_version += 1
                changed.add('auth_version')
            for key in changed - {'auth_version'}:
                setattr(previous, key, getattr(obj, key))
            previous.save(update_fields=changed)
            obj.__dict__.update(previous.__dict__)

    def get_deleted_objects(self, objs, request):
        # User deletion retains profiles/memberships; Django's physical collector is inapplicable.
        labels = [str(obj) for obj in objs]
        return labels, {str(self.model._meta.verbose_name_plural): len(labels)}, set(), []

    def delete_model(self, request, obj):
        deactivate_user(obj, delete=True)

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            for obj in queryset.select_for_update():
                deactivate_user(obj, delete=True)


class ProfileAdmin(ModelAdmin):
    list_display = ['user', 'display_name', 'sysid']
    readonly_fields = ['user', 'sysid', 'created_at', 'updated_at', 'revision']
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


class OrganizationForm(forms.ModelForm):
    owner = forms.ModelChoiceField(queryset=User.objects.filter(is_active=True), required=False)
    class Meta:
        model = Organization
        fields = ['name', 'slug', 'is_active']
    def clean(self):
        data = super().clean()
        if self.instance._state.adding and not data.get('owner'):
            self.add_error('owner', 'Select an existing owner.')
        return data


class OrganizationAdmin(ModelAdmin):
    form = OrganizationForm
    list_display = ['name', 'slug', 'is_active', 'sysid']
    readonly_fields = ['sysid', 'revision', 'created_at', 'updated_at']
    actions = None
    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (['slug'] if obj else [])
    def save_model(self, request, obj, form, change):
        if not change:
            created = create_organization(owner=form.cleaned_data['owner'], slug=obj.slug, name=obj.name)
            obj.__dict__.update(created.__dict__)
        else:
            obj.save()
    def has_delete_permission(self, request, obj=None):
        return False


class CatalogueAdmin(ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.concrete_fields if field.name not in {'label', 'description'}]
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


class AuditAdmin(ModelAdmin):
    list_display = ['created_at', 'table_name', 'operation', 'record_sysid', 'organization_id']
    exclude = ['changes']
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.concrete_fields if field.name != 'changes'] + ['visible_changes']
    @admin.display(description='Alterações legíveis')
    def visible_changes(self, obj):
        from skeleton.audit.presentation import visible_audit_changes
        return visible_audit_changes(obj)
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(User, PlatformUserAdmin)
admin.site.register(UserProfile, ProfileAdmin)
admin.site.register(Organization, OrganizationAdmin)
admin.site.register(TableDefinition, CatalogueAdmin)
admin.site.register(FieldDefinition, CatalogueAdmin)
admin.site.register(AuditEvent, AuditAdmin)
