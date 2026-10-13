ARG RELEASE_BASE=python:3.14-slim
FROM ${RELEASE_BASE}
USER root

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin

# Operator builds/provisions this OS base with XML/xmlsec libraries required by
# the locked SAML wheels. Release builds never contact an OS/package index.
RUN python -m venv /opt/venv
COPY wheels/ /opt/wheels/
COPY requirements.lock /opt/requirements.lock
RUN /opt/venv/bin/python -m pip install --disable-pip-version-check --no-index \
    --only-binary=:all: --find-links=/opt/wheels --require-hashes \
    -r /opt/requirements.lock \
    && /opt/venv/bin/python -m pip check

# Immutable application payload is part of the selected image as well as the
# same-generation read-only mounted release. No project source skeleton checkout.
COPY applications/ /opt/release/applications/
COPY config/ /opt/release/config/
COPY archives/ /opt/release/archives/
COPY retained/ /opt/release/retained/
COPY staticfiles/ /opt/release/staticfiles/
COPY inventory.json manage.py /opt/release/
RUN groupadd --gid 10001 skeleton && useradd --uid 10001 --gid 10001 \
    --no-create-home --shell /usr/sbin/nologin skeleton
USER skeleton
WORKDIR /opt/release
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "1800"]
