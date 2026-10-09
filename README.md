# Employee Attendance & Analytics API

A FastAPI and MongoDB service providing punch-in/out attendance tracking, manual audit trail regularizations, and reporting analytics.

## Requirements
- Python 3.11+
- MongoDB 6.0+

## Getting Started

1. Set up a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Configure environment variables (optional; defaults to `mongodb://localhost:27017` and `attendance_db`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. Seed initial sample documents (optional):
   ```bash
   python sample_seed.py
   ```

4. Start the server:
   ```bash
   uvicorn app.main:app --port 8000
   ```

## Architecture & Implementation Notes
- All endpoints conform strictly to `openapi.yaml` and the business rules (R1 - R10).
- Database indexes are created automatically and idempotently during application startup.
- Analytics endpoints are powered entirely by native MongoDB aggregation pipelines (`$densify`, `$setWindowFields`, `$lookup`, `$group`).
- Concurrent punch-ins and regularizations are protected against race conditions using unique database constraints and optimistic array sizing checks.
