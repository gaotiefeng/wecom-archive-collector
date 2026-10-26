FROM python:3.11-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home collector
COPY src ./src
COPY schema.sql .
USER collector
ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["run"]
