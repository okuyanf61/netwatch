FROM python:3.12-alpine

ARG VERSION=dev
ENV NETWATCH_VERSION=${VERSION} \
    NETWATCH_DB=/data/netwatch.db \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN adduser -D -H -u 1000 netwatch \
 && mkdir /data && chown netwatch:netwatch /data

COPY --chmod=0755 netwatch.py /app/netwatch.py

USER netwatch
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1

ENTRYPOINT ["python3", "/app/netwatch.py"]
