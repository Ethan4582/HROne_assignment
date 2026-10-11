# Attendance & Analytics API

FastAPI service for employee attendance tracking and reporting over MongoDB.

## Prerequisites

- Python 3.11+
- Docker & Docker Compose (or a local MongoDB 6.0+ instance)

## Running with Docker

Start MongoDB and the API together:

```bash
docker compose up -d
```

The API runs at `http://127.0.0.1:8000`. Interactive docs are available at `http://127.0.0.1:8000/docs`.

To shut down:

```bash
docker compose down
```

## Running Locally

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. Start MongoDB in Docker (port 27017):

```bash
docker run -d --name mongo -p 27017:27017 mongo:6.0
```

3. (Optional) Load sample seed data:

```bash
python sample_seed.py
```

4. Start the development server:

```bash
uvicorn app.main:app --port 8000 --reload
```

## Running Tests

Run the test suite against the running MongoDB instance:

```bash
pytest tests/ -v
```

All 19 unit and integration tests cover business rules (R1-R8), concurrency handling, aggregations, and MongoDB index scans.
