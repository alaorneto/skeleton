FROM python:3.14-slim AS core-builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxmlsec1 libxmlsec1-openssl xmlsec1 && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY pyproject.toml ./
COPY skeleton ./skeleton
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /core-wheel .

FROM core-builder AS project-assets
COPY . /project-source
RUN mkdir -p /project-assets/templates && if [ -d /project-source/templates ]; then cp -a /project-source/templates/. /project-assets/templates/; fi

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SKELETON_PROJECT_ROOT=/app
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libxml2 libxmlsec1 libxmlsec1-openssl xmlsec1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY --from=core-builder /core-wheel /core-wheel
RUN pip install --no-cache-dir --no-deps /core-wheel/*.whl && rm -rf /core-wheel
COPY config ./config
COPY applications ./applications
COPY examples ./examples
COPY manage.py ./
COPY --from=project-assets /project-assets/templates ./templates
RUN DJANGO_SETTINGS_MODULE=config.settings.build python manage.py collectstatic --noinput && useradd --uid 10001 --create-home skeleton && chown -R skeleton:skeleton /app/staticfiles
USER skeleton
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
