# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy app files
COPY app/ ./app
COPY data/ ./data

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn[standard] aiohttp beautifulsoup4 lxml pandas xlsxwriter jinja2 requests

# Expose port
EXPOSE 8222

# Run app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8222"]
