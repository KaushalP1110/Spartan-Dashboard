# Spartan Dashboard - one image that can run every automation (service
# "spartan-dashboard" in ../spartan/docker-compose.yml):
# Python (Reminder Sanity, Recall Status, Template Setter), JDK 21 + Maven
# (Spartan API, Cron Reporting), Chromium (the Python projects' PDF export).
FROM python:3.12-slim-bookworm

COPY --from=eclipse-temurin:21-jdk /opt/java/openjdk /opt/java/openjdk
COPY --from=maven:3.9-eclipse-temurin-21 /usr/share/maven /usr/share/maven
ENV JAVA_HOME=/opt/java/openjdk \
    MAVEN_HOME=/usr/share/maven \
    PATH=/opt/java/openjdk/bin:/usr/share/maven/bin:$PATH \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation socat tzdata curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Chromium won't start as root without --no-sandbox. The projects' PDF
# exporters look for "google-chrome" before "chromium", so this wrapper is
# picked up without changing their code.
RUN printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage --disable-gpu "$@"\n' \
      > /usr/local/bin/google-chrome \
 && chmod +x /usr/local/bin/google-chrome

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

# The projects themselves are mounted at /workspace (see docker-compose.yml).
WORKDIR /workspace/Spartan-Dashboard
EXPOSE 8765
ENTRYPOINT ["/docker-entrypoint.sh"]
