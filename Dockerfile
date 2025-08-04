FROM python:3.12

WORKDIR /app

COPY src/requirements.txt .

RUN pip install -r requirements.txt

COPY src/ .

CMD ["kopf", "run", "hpa_prescaler.py", "--all-namespaces",  "--liveness=http://0.0.0.0:8080/healthz", "--standalone"]

# enable json log format
# CMD ["kopf", "run", "hpa_prescaler.py", "--all-namespaces",  "--liveness=http://0.0.0.0:8080/healthz", "--standalone", "--log-format=json"]

# enable debug messages with --verbose
# CMD ["kopf","run","hpa_prescaler.py", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz","--verbose","--standalone"]
