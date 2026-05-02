FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py /app/
COPY docs/ /app/docs/

ENV PYTHONUNBUFFERED=1

EXPOSE 5800

CMD ["python", "app_webui.py", "--host", "0.0.0.0", "--port", "5800", "--root", "/media"]
