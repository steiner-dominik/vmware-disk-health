# Multi-arch image for the standalone deployment and the Home Assistant app.
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim
LABEL org.opencontainers.image.title="VMware Disk Health" \
      org.opencontainers.image.description="SMART and SSD health monitoring for VMware ESXi hosts" \
      org.opencontainers.image.source="https://github.com/steiner-dominik/vmware-disk-health" \
      org.opencontainers.image.licenses="MIT"

# Home Assistant writes /data/options.json as root without changing the
# container user, so the app runs as root; dropping privileges would leave it
# unable to read its own configuration.
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels vmware-disk-health \
    && rm -rf /wheels

ENV VDH_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"

ENTRYPOINT ["vmware-disk-health"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "8080"]
