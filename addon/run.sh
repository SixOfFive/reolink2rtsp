#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

bashio::log.info "starting reolink2rtsp"
exec python3 /opt/bootstrap.py
