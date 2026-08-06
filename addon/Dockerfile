ARG BUILD_FROM
FROM ${BUILD_FROM}

# Which revision of the project to install. Override with a tag or commit to
# pin the add-on to a known-good build.
ARG SOURCE_REF=main
ARG SOURCE_REPO=https://github.com/SixOfFive/reolink2rtsp

# python3 is all the bridge needs; py3-cryptography is optional but strongly
# wanted, because the camera encrypts the head of every video frame and the
# pure-Python AES fallback is far slower. Taking Alpine's prebuilt package
# avoids compiling Rust at image build time.
RUN apk add --no-cache python3 py3-cryptography

# The add-on directory is the Docker build context, so the package cannot be
# COPYed from the repository root - fetch it from the same repo instead. A
# rebuild is therefore also how the add-on updates.
RUN set -eux; \
    mkdir -p /opt; \
    wget -qO /tmp/src.tar.gz "${SOURCE_REPO}/archive/${SOURCE_REF}.tar.gz"; \
    tar -xzf /tmp/src.tar.gz -C /tmp; \
    mv /tmp/reolink2rtsp-*/reolink2rtsp /opt/reolink2rtsp; \
    rm -rf /tmp/src.tar.gz /tmp/reolink2rtsp-*; \
    python3 -c "import sys; sys.path.insert(0, '/opt'); import reolink2rtsp; print('installed', reolink2rtsp.__version__)"

COPY bootstrap.py /opt/bootstrap.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONPATH=/opt PYTHONUNBUFFERED=1

CMD [ "/run.sh" ]

LABEL \
    io.hass.name="reolink2rtsp" \
    io.hass.description="RTSP bridge for Reolink cameras that have no RTSP" \
    io.hass.type="addon"
