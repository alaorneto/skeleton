# syntax=docker/dockerfile:1
FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS core-builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxmlsec1 libxmlsec1-openssl xmlsec1 && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY pyproject.toml ./
COPY skeleton ./skeleton
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /core-wheel .

FROM core-builder AS project-assets
# Bind mount only during the RUN: the build context never becomes an image layer (#248).
RUN --mount=type=bind,target=/project-source \
    mkdir -p /project-assets/templates && \
    if [ -d /project-source/templates ]; then cp -a /project-source/templates/. /project-assets/templates/; fi

FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS runtime
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
USER skeleton
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
