# Personal proxy — Shadowsocks server (official shadowsocks-rust
# image) with the v2ray-plugin transport, following upstream's own
# docker/Dockerfile.v2ray recipe with the plugin version pinned for
# reproducible builds. The plugin serves WebSocket on the container's
# 8388; the gateway's nginx terminates the public TLS in front of it
# (see README.md). The password is NOT in this image: the entrypoint
# renders the server config from the PROXY_SS_PASSWORD environment
# variable at container start.
FROM ghcr.io/shadowsocks/ssserver-rust:latest

USER root

ARG V2RAY_PLUGIN_VERSION=v1.5.0
RUN cd /tmp && \
    wget -q https://github.com/shadowsocks/v2ray-plugin/releases/download/${V2RAY_PLUGIN_VERSION}/v2ray-plugin-linux-amd64-${V2RAY_PLUGIN_VERSION}.tar.gz && \
    tar -xf v2ray-plugin-linux-amd64-${V2RAY_PLUGIN_VERSION}.tar.gz && \
    rm v2ray-plugin-linux-amd64-${V2RAY_PLUGIN_VERSION}.tar.gz && \
    mv v2ray-plugin_linux_amd64 /usr/bin/v2ray-plugin && \
    chmod +x /usr/bin/v2ray-plugin

COPY entrypoint.sh /usr/local/bin/proxy-entrypoint.sh
RUN chmod +x /usr/local/bin/proxy-entrypoint.sh

USER nobody

EXPOSE 8388
ENTRYPOINT ["/usr/local/bin/proxy-entrypoint.sh"]
