# example of Dockerfile that builds release of electrumx-meowcoin-2.1.0
# ENV variables can be overrided on the `docker run` command

FROM debian:bookworm-slim

WORKDIR /

RUN apt-get update && \
        apt-get -y install python3 python3-pip python3-venv git cmake libleveldb-dev libssl-dev

RUN useradd -ms /bin/bash electrumx

RUN mkdir -p /db
RUN chown electrumx /db

USER electrumx
WORKDIR /home/electrumx

ENV VIRTUAL_ENV=/home/electrumx/.venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN python3 -m pip install --upgrade pip

ARG VERSION=2.1

ADD --chown=electrumx https://github.com/Meowcoin-Foundation/electrumx-meowcoin/archive/v${VERSION}.tar.gz .
RUN tar zxvf *.tar.gz
RUN rm *.tar.gz

WORKDIR /home/electrumx/electrumx-meowcoin-${VERSION}
RUN python3 -m pip install -r requirements.txt
RUN python3 setup.py install
WORKDIR /home/electrumx

RUN rm -r electrumx-meowcoin-${VERSION}

ENV SERVICES="ssl://:50002,tcp://:50001,wss://:50004"
ENV COIN=MEOWCOIN
ENV DB_DIRECTORY=/db
ENV DAEMON_URL="http://username:password@hostname:port/"
ENV PYTHONPATH=/usr/bin/python3
ENV MAX_SESSIONS=2000
ENV MAX_RECV=5000000
ENV ALLOW_ROOT=true
ENV MAX_SEND=1000000
ENV COST_SOFT_LIMIT=2000
ENV COST_HARD_LIMIT=20000
ENV REQUEST_SLEEP=3000
ENV INITIAL_CONCURRENT=50
ENV BANDWIDTH_UNIT_COST=5000
ENV REQUEST_TIMEOUT=60
ENV SESSION_TIMEOUT=1200
ENV CACHE_MB=2000
ENV EVENT_LOOP_POLICY=uvloop

ENV SSL_CERTFILE="${DB_DIRECTORY}/ssl_cert/server.crt"
ENV SSL_KEYFILE="${DB_DIRECTORY}/ssl_cert/server.key"

# !!README!!
# REQUIRED to connect with the larger electrumx meowcoin server network.
# Full service://(IP/URL):PORT is REQUIRED
# Example:
# env REPORT_SERVICES = ssl://meow4lyfe.com:50002,tcp://meow4lyfe.com:50001

EXPOSE 50004
EXPOSE 50001
EXPOSE 50002

VOLUME /db

RUN ulimit -n 1048576

CMD if [ -d "$DB_DIRECTORY/ssl_cert/" ]; then \
        echo "SSL certs exist."; \
    else \ 
        mkdir "$DB_DIRECTORY/ssl_cert" && \
        cd "$DB_DIRECTORY/ssl_cert" && \
        openssl genrsa -out server.key 2048 && \
        openssl req -new -key server.key -out server.csr -subj "/C=AU" && \
        openssl x509 -req -days 1825 -in server.csr -signkey server.key -out server.crt; \
    fi && electrumx_server

# build it with eg.: `docker build -t electrumx-meowcoin .`
# run it with eg.:
# `docker run -d --net=host -v /home/electrumx/db/:/db -e DAEMON_URL="http://youruser:yourpass@localhost:8766" -e REPORT_SERVICES=tcp://example.com:50001 electrumx-ravencoin`
# for a proper clean shutdown, send TERM signal to the running container eg.: `docker kill --signal="TERM" CONTAINER_ID`
