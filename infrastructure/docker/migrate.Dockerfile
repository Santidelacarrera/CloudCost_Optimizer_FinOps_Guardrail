FROM postgres:16-alpine
COPY migrations /migrations
COPY scripts/migrate.sh /migrate.sh
ENV MIGRATIONS_DIR=/migrations
CMD ["sh", "/migrate.sh"]
