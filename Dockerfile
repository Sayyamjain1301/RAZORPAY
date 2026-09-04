# Payment Reconciliation Agent — no setup beyond `docker build` + `docker run`.
#
#   docker build -t recon-agent .
#   docker run -p 8501:8501 recon-agent
#   # optional: pass a live key
#   docker run -p 8501:8501 -e ANTHROPIC_API_KEY=sk-... recon-agent
#
# Data is generated at first run from inside the container (data_gen.py),
# so no volumes are required for a basic demo.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

# python:3.11-slim has no curl by default -- use urllib instead of adding an
# apt dependency just for the healthcheck.
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
