FROM python:3.13-slim
WORKDIR /app
COPY tools/ ./tools/
COPY index.html data.json ./
EXPOSE 8790
CMD ["python3", "tools/serve.py"]
