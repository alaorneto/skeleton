"""Notification preferences of the signed-in person: page and session API (#53).

Both surfaces act only on ``request.user``'s rows: no user id ever comes from the request.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.views import SessionAPI, request_fields
from skeleton.common.exceptions import ConcurrentUpdate

from . import preferences
from .models import DEFAULT_TYPE

HOURS = tuple(range(24))


def _item(line, channel):
    state, row = line.channels[channel]
    return {
        "type_code": line.type_code,
        "label": line.label,
        "mandatory": line.mandatory,
        "channel": channel,
        "mode": state.mode,
        "digest_hour": state.digest_hour,
        "source": state.source,
        "sysid": row.sysid if row is not None else None,
        "revision": row.revision if row is not None else None,
    }


def _line(user, type_code):
    for line in preferences.preference_lines(user):
        if line.type_code == type_code:
            return line
    raise Http404


class PreferencesAPI(SessionAPI):
    def get(self, request):
        lines = preferences.preference_lines(request.user)
        return Response({"results": [_item(line, channel) for line in lines for channel in preferences.CHANNELS]})


class PreferenceAPI(SessionAPI):
    def _respond(self, request, type_code, channel):
        item = _item(_line(request.user, type_code), channel)
        response = Response(item)
        if item["revision"] is not None:
            response["ETag"] = f'"{item["revision"]}"'
        return response

    def _channel(self, type_code, channel):
        if channel not in preferences.CHANNELS:
            raise Http404
        if type_code != DEFAULT_TYPE and preferences.known_type(type_code) is None:
            raise Http404

    def get(self, request, type_code, channel):
        self._channel(type_code, channel)
        return self._respond(request, type_code, channel)

    @transaction.atomic
    def put(self, request, type_code, channel):
        self._channel(type_code, channel)
        data = request_fields(request)
        if set(data) - {"mode", "digest_hour"} or "mode" not in data:
            raise BoundaryError("invalid_request", "Envie mode e, opcionalmente, digest_hour.")
        header = request.headers.get("If-Match")
        current = _line(request.user, type_code).channels[channel][1]
        if current is not None and header is None:
            raise BoundaryError("revision_required", "Envie If-Match com a revisão atual.", 428)
        expected = parse_revision(header) if header is not None else None
        try:
            preferences.set_preference(
                request.user,
                type_code=type_code,
                channel=channel,
                mode=data["mode"],
                digest_hour=data.get("digest_hour"),
                expected_revision=expected,
            )
        except ConcurrentUpdate:
            raise BoundaryError("revision_conflict", "A preferência foi alterada por outra operação.", 412) from None
        return self._respond(request, type_code, channel)

    @transaction.atomic
    def delete(self, request, type_code, channel):
        self._channel(type_code, channel)
        expected = parse_revision(request.headers.get("If-Match"))
        try:
            preferences.clear_preference(request.user, type_code=type_code, channel=channel, expected_revision=expected)
        except ConcurrentUpdate:
            raise BoundaryError("revision_conflict", "A preferência foi alterada por outra operação.", 412) from None
        return Response(status=204)


def _field(prefix, type_code, channel):
    return f"{prefix}:{type_code}:{channel}"


def _submitted(request, lines):
    """Changes the form asks for: ``[(line, channel, mode, hour, expected_revision)]``."""
    allowed = {"csrfmiddlewaretoken"}
    changes = []
    for line in lines:
        for channel in preferences.CHANNELS:
            names = (_field("mode", line.type_code, channel), _field("revision", line.type_code, channel))
            hour_name = _field("hour", line.type_code, channel)
            allowed.update((*names, hour_name))
            state, row = line.channels[channel]
            mode = request.POST.get(names[0])
            if mode is None:
                continue
            raw_hour = request.POST.get(hour_name)
            try:
                hour = int(raw_hour) if raw_hour not in (None, "") else state.digest_hour
                raw_revision = request.POST.get(names[1], "")
                expected = int(raw_revision) if raw_revision else None
            except ValueError:
                raise ValidationError("Valor inválido.") from None
            if row is None and expected is None and mode == state.mode and hour == state.digest_hour:
                continue  # Untouched default: no row is written.
            if row is not None and expected == row.revision and (mode, hour) == (row.mode, row.digest_hour):
                continue
            changes.append((line, channel, mode, hour, expected))
    if set(request.POST) - allowed:
        raise ValidationError("Campo não permitido.")
    return changes


@login_required
@require_http_methods(["GET", "HEAD", "POST"])
def preferences_page(request):
    lines = preferences.preference_lines(request.user)
    errors, status = [], 200
    if request.method == "POST":
        try:
            with transaction.atomic():
                for line, channel, mode, hour, expected in _submitted(request, lines):
                    preferences.set_preference(
                        request.user,
                        type_code=line.type_code,
                        channel=channel,
                        mode=mode,
                        digest_hour=hour,
                        expected_revision=expected,
                    )
        except (ConcurrentUpdate, IntegrityError):
            errors, status = ["As preferências foram alteradas em outra aba. Revise e salve de novo."], 409
        except ValidationError as error:
            errors, status = error.messages, 400
        else:
            messages.success(request, "Preferências de notificação salvas.")
            return redirect("notifications:preferences")
        lines = preferences.preference_lines(request.user)
    rows = [
        {
            "type_code": line.type_code,
            "label": line.label,
            "mandatory": line.mandatory,
            "cells": [
                {
                    "channel": channel,
                    "mode": line.channels[channel][0].mode,
                    "hour": line.channels[channel][0].digest_hour,
                    "source": line.channels[channel][0].source,
                    "revision": line.channels[channel][1].revision if line.channels[channel][1] else "",
                    "modes": preferences.MODES[channel],
                    "locked": line.mandatory and channel == "in_app",
                    "mode_name": _field("mode", line.type_code, channel),
                    "hour_name": _field("hour", line.type_code, channel),
                    "revision_name": _field("revision", line.type_code, channel),
                }
                for channel in preferences.CHANNELS
            ],
        }
        for line in lines
    ]
    return render(
        request,
        "account/notification_preferences.html",
        {
            "title": "Notificações",
            "rows": rows,
            "errors": errors,
            "hours": HOURS,
            "mode_labels": dict(preferences.MODE_LABELS),
            "channel_labels": dict(preferences.CHANNEL_LABELS),
        },
        status=status,
    )
