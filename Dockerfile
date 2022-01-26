FROM python:3.10-slim as base

RUN apt-get update && \
    apt-get install --no-install-recommends --no-upgrade -y \
        build-essential \
        gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /wheels

COPY requirements.txt /wheels

RUN pip install -U pip \
   && pip wheel -r requirements.txt

FROM python:3.10-slim

COPY --from=base /wheels /wheels

# Required for time-sync-status
RUN apt-get update && \
    apt-get install --no-install-recommends --no-upgrade -y \
        chrony && \
    rm -rf /var/lib/apt/lists/*

RUN pip install -U pip \
       && pip install -r /wheels/requirements.txt \
                      -f /wheels \
       && rm -rf /root/.cache/pip/*

RUN mkdir /src
WORKDIR /src
COPY . /src
RUN pip install -e .

CMD ["riasc-operator"]
