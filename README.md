# Attendance & Analytics API

FastAPI service for employee attendance tracking and reporting over MongoDB.

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ instance (local, Docker, or Atlas)

## Running the Application

1. Set up a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. Configure environment variables (optional; defaults to localhost):

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

3. (Optional) Start a local MongoDB container for testing:

```bash
docker run -d --name mongo -p 27017:27017 mongo:6.0
```

4. (Optional) Load sample seed data:

```bash
python sample_seed.py
```

5. Start the API server:

```bash
uvicorn app.main:app --port 8000
```

The API runs at `http://127.0.0.1:8000`. Interactive docs are available at `http://127.0.0.1:8000/docs`. All indexes are created automatically at startup.

## Running Tests

Run the test suite against your running MongoDB instance:

```bash
pytest tests/ -v
```

All 19 unit and integration tests cover business rules (R1-R8), concurrency handling, aggregations, and index scans.
