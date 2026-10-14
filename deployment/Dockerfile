# Recipe only: no container runtime was available for validation on this host.
FROM python:3.13-slim
WORKDIR /app
COPY cortex /app/cortex
COPY config/cortex.public.example.json config/cortex.public.offline.json config/tools.example.json /app/config/
RUN useradd --uid 10001 --create-home cortex && mkdir /data && chown cortex:cortex /data
USER 10001:10001
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
ENTRYPOINT ["python", "-m", "cortex"]
# Supply web --public --origin https://YOUR_HOST ... or web-reminders ...
