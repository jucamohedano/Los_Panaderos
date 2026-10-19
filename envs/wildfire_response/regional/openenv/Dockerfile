FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "openenv==0.7.0" "uvicorn>=0.30,<1"
COPY envs/__init__.py envs/__init__.py
COPY envs/wildfire_response envs/wildfire_response
COPY simulator/__init__.py simulator/engine.py simulator/terrain.py simulator/contacts.py simulator/policy.py simulator/oracle.py simulator/
COPY simulator/static/maps/brunete-illustrated.json simulator/static/maps/brunete-illustrated.json
EXPOSE 7860
CMD ["python", "-m", "envs.wildfire_response.regional.openenv.server", "--host", "0.0.0.0", "--port", "7860"]
