FROM python:3.12-slim
WORKDIR /bridge
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# 845 = factorio uid; bridge writes to the shared factorio-data volume
USER 845:845
CMD ["python", "-m", "app.main"]
