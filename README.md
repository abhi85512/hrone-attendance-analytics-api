# Employee Attendance & Analytics API

A robust Employee Attendance and Analytics API built with **Python 3.10+**, **FastAPI**, and **MongoDB 6.0+**. 

The system handles daily attendance tracking (punch-in/punch-out), overnight shift adjustments, audit-trailed manual corrections, and rich organizational analytics powered by MongoDB aggregation pipelines.

---

## Features

- **Employee Management**: Create and list employees with department filtering and 1-indexed pagination.
- **Punch-In / Punch-Out**:
  - Millisecond-precision epoch API with second-level truncation (Rule R1).
  - Multi-shift support (General, Evening, Night) with overnight shift crossing detection (Rule R2).
  - Grace period enforcement (15 mins) and automatic penalty status computation (Rule R3).
  - Concurrency-safe: Unique compound indexes prevent duplicate punch-ins; atomic `find_one_and_update` prevents duplicate punch-outs.
- **Attendance Corrections**:
  - Full audit trail using MongoDB `$push` to retain complete edit history without overwriting previous versions (Rule R5).
- **Advanced Analytics via Aggregations**:
  - **Employee Monthly Summary**: Total days, present days, late count, overtime hours (using `ROUND_HALF_UP`), and attendance percentage (Rule R8).
  - **Department Summary**: Preserves inactive/zero-log employees using left-outer `$lookup` from the `employees` collection (Rule R9).
  - **Late Leaderboard**: Standard competition ranking (`1, 2, 2, 4`) using `$setWindowFields` with `$rank` and tie preservation (Rule R7).
  - **Department 30-Day Trend**: Gap-filled daily attendance metrics using MongoDB `$densify` (Rule R10).
- **Explainability Endpoint**: `GET /admin/explain/{endpoint}` returns query execution stats and index usage.

---

## Local Setup

### 1. Prerequisites
- Python 3.10 or higher
- MongoDB 6.0 or higher (e.g. MongoDB Atlas or local MongoDB instance)

### 2. Virtual Environment & Dependencies
```bash
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows PowerShell:
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the project root:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
PORT=8000
```
*(Note: `.env` and local secrets are excluded from source control)*

### 4. Seed Sample Data
```bash
python sample_seed.py
```

### 5. Start the API
```bash
uvicorn app.main:app --port 8000 --reload
```
Once started, interactive API documentation is available at:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## Core Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check and database connectivity probe |
| `POST` | `/employees` | Register a new employee |
| `GET` | `/employees` | List employees with department filter and pagination |
| `POST` | `/attendance/punch-in` | Record daily punch-in (concurrency safe) |
| `POST` | `/attendance/punch-out` | Record daily punch-out and compute overtime |
| `GET` | `/attendance` | Query attendance logs with date range and filters |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct attendance records with immutable audit trail |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance breakdown |
| `GET` | `/analytics/departments/summary` | Department summary with zero-log headcount retention |
| `GET` | `/analytics/leaderboard/late` | Competition-ranked late arrivals leaderboard |
| `GET` | `/analytics/departments/{department}/trend` | 30-day attendance trend with densified dates |
| `GET` | `/admin/explain/{endpoint}` | MongoDB query execution plan and winning index stats |

---

## Submission Artifacts

- `app/main.py`: Complete application implementation and database migrations.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect analysis and corrections for starter code.
- `DECISIONS.md`: Design rationales covering indexing, race condition handling, rankings, and scalability.
- `README.md`: Setup and architecture overview.
