FROM ubuntu:14.04

WORKDIR /app

RUN apt-get update -qq && apt-get install -y python-pip python-dev libffi-dev libssl-dev libsqlite3-dev \
        libldap2-dev libsasl2-dev git curl make build-essential zlib1g-dev libbz2-dev \
        libreadline-dev

ADD . /app/

RUN bin/barbican.sh install
