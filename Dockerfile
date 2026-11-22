# syntax=docker/dockerfile:1
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS core-builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxmlsec1 libxmlsec1-openssl xmlsec1 && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY pyproject.toml ./
COPY skeleton ./skeleton
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /core-wheel .
# The agent's own distribution (#309), installed in the agent image: its hooks run without PYTHONPATH.
COPY deployment_agent ./deployment_agent
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /agent-wheel ./deployment_agent

FROM core-builder AS project-assets
# Bind mount only during the RUN: the build context never becomes an image layer (#248).
RUN --mount=type=bind,target=/project-source \
    mkdir -p /project-assets/templates && \
    if [ -d /project-source/templates ]; then cp -a /project-source/templates/. /project-assets/templates/; fi

# The deployment agent of container deployments (#652): the same Python and system libraries as
# the runtime (the releases it builds run there), the PostgreSQL client for backups and the
# project files the first release is built from. It never needs the Docker socket.
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS agent
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxmlsec1 libxmlsec1-openssl xmlsec1 postgresql-common \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=core-builder /core-wheel /core-wheel
COPY --from=core-builder /agent-wheel /agent-wheel
RUN pip install --no-cache-dir --no-deps /core-wheel/*.whl /agent-wheel/*.whl && rm -rf /core-wheel /agent-wheel
# The official publisher's public key (#656): a fork replaces it with SKELETON_AGENT_PUBLISHER_KEY.
COPY deploy/release/publishers /opt/agent/publishers
COPY config /opt/project/config
COPY applications /opt/project/applications
COPY examples /opt/project/examples
COPY manage.py /opt/project/manage.py
COPY --from=project-assets /project-assets/templates /opt/project/templates
RUN useradd --uid 10001 --create-home skeleton \
    && mkdir -p /agent/private /agent/state /releases /control /control-state /instance/keys \
    && chown -R skeleton:skeleton /agent /releases /control /control-state /instance \
    && chmod 700 /agent/private /agent/state /instance /instance/keys
USER skeleton
CMD ["python", "-m", "deployment_agent.bootstrap"]

FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SKELETON_PROJECT_ROOT=/app
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxmlsec1 libxmlsec1-openssl xmlsec1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY --from=core-builder /core-wheel /core-wheel
RUN pip install --no-cache-dir --no-deps /core-wheel/*.whl && rm -rf /core-wheel
COPY config ./config
COPY applications ./applications
# The example app is opt-in at runtime; --build-arg INCLUDE_EXAMPLES=false leaves it out (#318).
ARG INCLUDE_EXAMPLES=true
COPY examples ./examples
RUN if [ "$INCLUDE_EXAMPLES" != "true" ]; then rm -rf ./examples && mkdir ./examples && touch ./examples/__init__.py; fi
COPY manage.py ./
COPY --from=project-assets /project-assets/templates ./templates
RUN DJANGO_SETTINGS_MODULE=config.settings.build python manage.py collectstatic --noinput && useradd --uid 10001 --create-home skeleton && chown -R skeleton:skeleton /app/staticfiles
# Instance SQLite and master key (#148): a private named volume mounted here keeps them across containers.
RUN mkdir -p /instance/keys && chown -R skeleton:skeleton /instance && chmod 700 /instance /instance/keys
# Mount points of the agent's volumes (#654), owned by the service user so a volume created
# empty here starts writable: files is written by the services; releases and control are read-only.
RUN mkdir -p /files /releases /control /control-state && chown skeleton:skeleton /files /releases /control /control-state
# The launcher (#651) runs the agent's active release and obeys its stop and restart requests;
# only the standard library, outside the managed releases.
COPY deployment_agent/__init__.py deployment_agent/control.py deployment_agent/launcher.py /opt/launcher/deployment_agent/
USER skeleton
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--no-control-socket"]
