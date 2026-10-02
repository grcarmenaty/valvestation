FROM python:3.12-slim

WORKDIR /opt/valvestation
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

WORKDIR /station
ENV VALVESTATION_HOST=0.0.0.0
EXPOSE 508
ENTRYPOINT ["valvestation"]
