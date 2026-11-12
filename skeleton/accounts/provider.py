"""allauth loads ``<app>.provider.provider_classes``: the LDAP provider of #32, without a new app label."""

from skeleton.accounts.ldap.provider import LDAPProvider

provider_classes = [LDAPProvider]
