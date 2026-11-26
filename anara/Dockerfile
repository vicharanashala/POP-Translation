# Public (keyword-only) Source Mapper image: the app code and the Python packages it needs, nothing else. No torch, no
# MongoDB, no .env, no data: the data folder is mounted read-only at run time (docker-compose.yml). Build: ./deploy.sh build
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPYCACHEPREFIX=/tmp/pyc PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# same versions as the .venv that built the data files (the .pkl files are numpy/pandas pickles)
RUN pip install numpy==2.3.5 scipy==1.18.0 pandas==3.0.5 scikit-learn==1.9.0 fastapi==0.136.1 uvicorn==0.47.0 h11==0.16.0 \
        pydantic==2.13.4 openpyxl==3.1.5 pymongo==4.17.0 PyMuPDF==1.27.2.3 python-dotenv==1.2.2 langdetect==1.0.9 \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /auth /runs /data && chown 10001:10001 /auth /runs && chmod 700 /auth
WORKDIR /app
COPY --chown=root:root *.py *.html *.xlsx /app/anara/
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8050/health', timeout=3)"]
EXPOSE 8050
CMD ["python", "anara/app.py"]
