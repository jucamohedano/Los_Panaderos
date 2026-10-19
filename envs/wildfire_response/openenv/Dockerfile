FROM python:3.12-slim
WORKDIR /app
COPY simulator/__init__.py simulator/engine.py simulator/terrain.py simulator/contacts.py simulator/policy.py simulator/oracle.py /app/simulator/
COPY simulator/static/maps/brunete-illustrated.json /app/simulator/static/maps/
COPY envs /app/envs
RUN pip install --no-cache-dir '/app/envs/wildfire_response[openenv]'
ENV MAX_CONCURRENT_ENVS=1
EXPOSE 8000
CMD ["python", "-m", "envs.wildfire_response.openenv.server"]
