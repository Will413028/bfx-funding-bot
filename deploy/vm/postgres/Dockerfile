FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2 AS pgbackrest-build

ARG PG_BACKREST_VERSION=2.59.1
ARG PG_BACKREST_SHA256=1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d

RUN apk add --no-cache \
        build-base \
        bzip2-dev \
        curl \
        lz4-dev \
        meson \
        ninja \
        openssl-dev \
        pkgconf \
        postgresql-dev \
        python3 \
        libxml2-dev \
        zlib-dev \
        zstd-dev

WORKDIR /build

RUN curl --fail --location --show-error \
        --output pgbackrest.tar.gz \
        "https://github.com/pgbackrest/pgbackrest/releases/download/release%2F${PG_BACKREST_VERSION}/pgbackrest-${PG_BACKREST_VERSION}.tar.gz" \
    && printf '%s  %s\n' "${PG_BACKREST_SHA256}" pgbackrest.tar.gz > pgbackrest.sha256 \
    && sha256sum -c pgbackrest.sha256 \
    && mkdir source \
    && tar --extract --gzip --file pgbackrest.tar.gz \
        --directory source --strip-components=1

RUN meson setup pgbackrest-build source \
        -Dlibssh2=disabled \
        -Dlibsystemd=disabled \
        -Dlibzstd=enabled
RUN ninja -C pgbackrest-build
RUN meson test -C pgbackrest-build --suite smoke --print-errorlogs
RUN install -D -m 0755 pgbackrest-build/src/pgbackrest /out/pgbackrest \
    && ldd /out/pgbackrest \
        | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^\//) {name = $i; sub(/^.*\//, "", name); sub(/\(.*/, "", name); print name}}' \
        | sort -u > /out/runtime-libraries

FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2

ARG PG_BACKREST_VERSION=2.59.1
ARG PG_BACKREST_SHA256=1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d

LABEL org.bfx.postgresql.base-digest="sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2" \
      org.bfx.pgbackrest.version="${PG_BACKREST_VERSION}" \
      org.bfx.pgbackrest.source-sha256="${PG_BACKREST_SHA256}"

COPY --from=pgbackrest-build /out/pgbackrest /usr/local/bin/pgbackrest
COPY --from=pgbackrest-build /out/runtime-libraries /tmp/pgbackrest-runtime-libraries
COPY --from=pgbackrest-build /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY deploy/vm/postgres/dr-restore-entrypoint.sh /usr/local/bin/bfx-dr-restore
COPY deploy/vm/pgbackrest/pgbackrest.conf /etc/pgbackrest/pgbackrest.conf

RUN runtime_packages="$(sed 's/^/so:/' /tmp/pgbackrest-runtime-libraries | tr '\n' ' ')" \
    && apk add --no-cache jq ${runtime_packages} \
    && chmod 0755 /usr/local/bin/pgbackrest \
        /usr/local/bin/docker-entrypoint.sh \
        /usr/local/bin/bfx-dr-restore \
    && mkdir -p /etc/pgbackrest/conf.d \
        /var/spool/pgbackrest \
        /var/log/pgbackrest \
        /dr-evidence \
    && chown -R postgres:postgres \
        /etc/pgbackrest \
        /var/spool/pgbackrest \
        /var/log/pgbackrest \
        /dr-evidence \
    && rm /tmp/pgbackrest-runtime-libraries

RUN /usr/local/bin/pgbackrest version

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
