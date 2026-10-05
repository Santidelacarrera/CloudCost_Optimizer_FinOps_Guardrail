FROM postgres:16-alpine
COPY migrations /migrations
COPY scripts/migrate.sh /migrate.sh
# Robustez en Windows: si Git convirtió los finales de línea a CRLF, los restablece a LF.
RUN sed -i 's/\r$//' /migrate.sh /migrations/*.sql /migrations/seed/*.sql
ENV MIGRATIONS_DIR=/migrations
CMD ["sh", "/migrate.sh"]
