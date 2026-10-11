# Attendance & Analytics API

[![Architecture Decisions](https://img.shields.io/badge/Architecture-DECISIONS.md-bc8c74?style=flat-square)](./DECISIONS.md)
[![Defect Review](https://img.shields.io/badge/Audit-REVIEW.md-4a5568?style=flat-square)](./REVIEW.md)
[![Python](https://img.shields.io/badge/Python-3.11+-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0+-47a248?style=flat-square&logo=mongodb&logoColor=white)](https://www.mongodb.com/)

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
