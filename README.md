# Employee Attendance & Analytics API

FastAPI and MongoDB backend service implementing attendance tracking, overnight shift calculations, atomic race safety, and MongoDB aggregation analytics according to business rules R1 – R10.

---

## 1. Prerequisites
- Python 3.10+
- MongoDB 6.0+

---

## 2. Configuration & Environment Variables
The application reads configuration from environment variables (or falls back to local defaults):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Database name (default: `attendance_db`)

All required indexes on `employees` and `attendance_logs` are created idempotently at startup via the FastAPI lifespan handler.

---

## 3. Running Locally

### Step 1: Install dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Start the application
```bash
uvicorn app.main:app --port 8000
```
- API Base URL: `http://localhost:8000`
- Interactive API Docs (Swagger): `http://localhost:8000/docs`
- Health Check: `GET http://localhost:8000/health` (returns 200 when MongoDB is connected, else 503)

---

## 4. Deliverables Summary
- `app/main.py`: Complete FastAPI application with all helpers, schemas, and endpoints.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Comprehensive audit and fix log for starter code defects.
- `DECISIONS.md`: Design rationale covering indexes, race safety, leaderboard ties, headcount, and 100x scaling strategy.
