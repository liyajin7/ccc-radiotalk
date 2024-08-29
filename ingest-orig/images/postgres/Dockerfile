ARG POSTGRES_VERSION
ARG POSTGIS_VERSION

FROM postgres:${POSTGRES_VERSION}

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates krb5-user kstart \
    postgis ${POSTGIS_VERSION} pgtop \
    \
    ogdi-bin gdal-bin geoip-bin proj-bin gdal-data proj-data geoip-database \
    publicsuffix
RUN rm -rf /var/lib/apt/lists/*
