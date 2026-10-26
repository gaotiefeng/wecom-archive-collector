FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libcurl4 libssl3 libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG WITH_S3=false
RUN pip install --no-cache-dir -r requirements.txt && if [ "$WITH_S3" = "true" ]; then pip install --no-cache-dir 'boto3>=1.34'; fi
RUN useradd --uid 10001 --create-home collector && mkdir -p /app/var/files && chown -R collector:collector /app/var
COPY src ./src
COPY schema.sql .
USER collector
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["run"]
