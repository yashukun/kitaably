FROM postgres:17-alpine
COPY docker/bootstrap.sh docker/migrate.sh docker/buckets.sh docker/seed.sh /usr/local/bin/
COPY migrations/   /migrations/
COPY docker/buckets.sql /buckets.sql
COPY seed.sql           /seed.sql
RUN chmod 0755 /usr/local/bin/bootstrap.sh /usr/local/bin/migrate.sh \
    /usr/local/bin/buckets.sh   /usr/local/bin/seed.sh
USER postgres

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]
CMD ["migrate", "buckets"]
