FROM python:3.12-slim

# Run as non-root user
RUN groupadd -r diagnosis && useradd -r -g diagnosis -d /app -s /usr/sbin/nologin diagnosis

# Remove tools that could be abused if the container were compromised
RUN apt-get purge -y --auto-remove \
        curl wget netcat-openbsd ssh openssh-client \
    2>/dev/null; \
    rm -f /usr/bin/ssh /usr/bin/curl /usr/bin/wget /usr/bin/nc; \
    true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN chown -R diagnosis:diagnosis /app

USER diagnosis

EXPOSE 3000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3000"]
