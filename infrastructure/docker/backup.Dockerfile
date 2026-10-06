FROM postgres:16-alpine
RUN apk add --no-cache aws-cli
COPY scripts/backup.sh scripts/restore.sh scripts/verify_backup.sh scripts/backup_loop.sh /scripts/
# Robustez en Windows: si Git convirtió los finales de línea a CRLF, los restablece a LF.
RUN sed -i 's/\r$//' /scripts/*.sh && chmod +x /scripts/*.sh && mkdir /backups && chown postgres /backups
USER postgres
ENV BACKUP_DIR=/backups
CMD ["sh", "/scripts/backup_loop.sh"]
