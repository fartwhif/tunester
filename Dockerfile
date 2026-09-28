FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    procps \
    iptables \
    wireguard \
    iproute2 \
    && curl -sL -o /usr/local/bin/yt-dlp \
        https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN mkdir -p /app/data /etc/wireguard \
    && cp wireguard/wg0.conf.template /etc/wireguard/wg0.conf.template \
    && chmod +x entrypoint.sh

EXPOSE 8102

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["--serve", "--host", "0.0.0.0", "--port", "8102"]
