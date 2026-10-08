# Demo microservices (orders / inventory / payments) + load generator. Standard library only.
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY demo/services.py scripts/loadgen.py ./
# role and port come from the compose `command`
ENTRYPOINT ["python"]
