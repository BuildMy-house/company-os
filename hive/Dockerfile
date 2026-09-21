FROM elixir:1.20-slim

WORKDIR /app
ENV MIX_ENV=prod PORT=4100

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY mix.exs mix.lock ./
RUN mix deps.get --only prod
COPY lib ./lib
RUN mix compile

EXPOSE 4100
CMD ["mix", "run", "--no-halt"]
