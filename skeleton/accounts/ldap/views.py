"""``/accounts/ldap/<provider_id>/login/`` (#32): username and password checked against the directory.

POST only with CSRF; the password is a sensitive POST parameter (never in error reports).
Two rate limits apply before the directory is contacted: per client IP and per provider and
username. Every failure, from an unknown user to a wrong password or a directory outage,
shows the same neutral message. A valid bind becomes an allauth ``SocialLogin`` and goes
through ``complete_social_login``: the usual ``pre_social_login`` checks, MFA and JIT apply.
"""

from __future__ import annotations

from django import forms
from django.http import Http404
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters

from . import client

NEUTRAL = "Usuário ou senha inválidos."
IP_LIMIT = (20, 600)  # attempts per client IP in 10 minutes
USER_LIMIT = (5, 600)  # attempts per provider and username in 10 minutes


class LDAPLoginForm(forms.Form):
    username = forms.CharField(label="Usuário do diretório", max_length=256, strip=True)
    password = forms.CharField(
        label="Senha", strip=False, max_length=1024, widget=forms.PasswordInput(render_value=False)
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)


def _binding(provider_id):
    from skeleton.accounts.providers import configured_providers

    binding = configured_providers().get(provider_id)
    if binding is None or binding.protocol != "ldap":
        raise Http404
    return binding


def _limited(request, provider_id, username):
    from skeleton.common.ratelimit import hit

    address = request.META.get("REMOTE_ADDR", "")
    checks = (
        (f"ldap-login:ip:{address}", IP_LIMIT),
        (f"ldap-login:{provider_id}:{username.strip().lower()}", USER_LIMIT),
    )
    return any(not hit(key, limit=limit, window_s=window).allowed for key, (limit, window) in checks)


@method_decorator([never_cache, csrf_protect, sensitive_post_parameters("password")], name="dispatch")
class LDAPLoginView(View):
    template = "account/ldap_login.html"

    def get(self, request, provider_id):
        binding = _binding(provider_id)
        form = LDAPLoginForm(initial={"next": request.GET.get("next", "")})
        return render(request, self.template, {"form": form, "provider_name": binding.app.get("name")})

    def post(self, request, provider_id):
        from allauth.socialaccount.adapter import get_adapter
        from allauth.socialaccount.helpers import complete_social_login

        binding = _binding(provider_id)
        form = LDAPLoginForm(request.POST)
        context = {"form": form, "provider_name": binding.app.get("name")}
        if not form.is_valid():
            return render(request, self.template, context, status=400)
        username, password = form.cleaned_data["username"], form.cleaned_data["password"]
        if _limited(request, provider_id, username):
            response = render(
                request, self.template, {**context, "error": "Muitas tentativas. Tente de novo mais tarde."}, status=429
            )
            response["Retry-After"] = "600"
            return response
        try:
            options = client.options_from(binding.app["settings"]["directory"], binding.app["settings"]["options"])
            person = client.authenticate(options, binding.app.get("secret") or "", username, password)
        except (client.InvalidCredentials, client.DirectoryError, ValueError):
            form.add_error(None, NEUTRAL)
            return render(request, self.template, context, status=401)
        provider = get_adapter().get_provider(request, provider_id)
        sociallogin = provider.sociallogin_from_response(
            request,
            {"uid": person.uid, "email": person.email, "first_name": person.first_name, "last_name": person.last_name},
        )
        sociallogin.state = {"process": "login", "next": form.cleaned_data.get("next") or ""}
        if request.user.is_authenticated and request.POST.get("process") == "connect":
            # Connecting requires a recent authentication; pre_social_login enforces it.
            sociallogin.state["process"] = "connect"
        return complete_social_login(request, sociallogin)


login = LDAPLoginView.as_view()
