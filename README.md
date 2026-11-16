# Student Academic Tracking & Analytics (SATA)

A comprehensive full-stack web application for managing and analyzing student academic performance. Built with FastAPI backend and React frontend, this system provides real-time analytics, GPA tracking, and performance insights for educational institutions.

> All seed and test data in this repository is synthetic.

## 🎯 Features

### Student Dashboard
- **Personalized Overview**: Dynamic dashboard showing current GPA, semester performance, and academic trends
- **Performance Analytics**: Visual charts and graphs for GPA trends, subject-wise performance, and semester comparisons
- **Subject Tracking**: Detailed breakdown of subject scores, credits, and grades
- **Weak/Strong Analysis**: Automatic identification of strengths and areas for improvement

### Authentication & Security
- **Student ID-based Login**: Secure authentication using unique student IDs
- **JWT Token Management**: Stateless authentication with refresh tokens
- **Role-based Access Control**: Protected routes and API endpoints
- **Password Hashing**: Bcrypt-based password security

### Advanced Analytics

> **STALE:** This feature set (GPA, semester and subject analytics) was removed. See the Skills, Roadmap and Interview modules.
- **GPA Calculation**: Automatic GPA computation on 10.0 scale with conversion utilities
- **Semester Trends**: Track academic performance across multiple semesters
- **Comparative Analysis**: Benchmark against class averages and historical data
- **Subject Performance**: Detailed subject-wise analytics with credit weightage

### User Experience
- **Dark Mode**: Toggle between light and dark themes
- **Responsive Design**: Mobile-first, fully responsive UI
- **Loading States**: Smooth loading animations and skeleton screens
- **Error Handling**: User-friendly error messages and validation

## 🛠️ Tech Stack

### Backend
- **Framework**: FastAPI 0.109.0
- **Database**: PostgreSQL 15
- **ORM**: SQLAlchemy 2.0.25
- **Migrations**: Alembic 1.13.1
- **Authentication**: JWT (python-jose) + Bcrypt (passlib)
- **Validation**: Pydantic 2.5.3
- **API Testing**: Pytest, HTTPx
- **Rate Limiting**: SlowAPI
- **Logging**: Structured JSON logging

### Frontend
- **Framework**: React 18.2.0 + TypeScript 5.2.2
- **Build Tool**: Vite 5.0.0
- **Routing**: React Router 6.20.0
- **State Management**: TanStack Query 5.90.19
- **Styling**: Tailwind CSS 3.3.5
- **Charts**: Recharts 2.10.1, Chart.js 4.5.1
- **Animations**: Framer Motion 12.26.2
- **HTTP Client**: Axios 1.6.2
- **Icons**: Lucide React

## 📋 Prerequisites

- **Python**: 3.9 or higher
- **Node.js**: 18.0 or higher
- **PostgreSQL**: 15 or higher
- **Docker** (optional): For containerized database

## 🚀 Quick Start

### 1. Clone the Repository
```bash
git clone <repository-url>
cd fullstack
```

### 2. Database Setup

#### Option A: Using Docker (Recommended)
```bash
docker-compose up -d
```

#### Option B: Manual PostgreSQL Setup
```bash
# Install PostgreSQL 15
# Create database
psql -U postgres
CREATE DATABASE student_tracker;
CREATE USER studentadmin WITH PASSWORD 'studentpass123';
GRANT ALL PRIVILEGES ON DATABASE student_tracker TO studentadmin;
\q
```

### 3. Backend Setup

```bash
# Navigate to backend directory
cd backend

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your configuration

# Run database migrations
alembic upgrade head

# Start the development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Backend will be available at `http://localhost:8000`
- API Documentation (Swagger): `http://localhost:8000/docs`
- Alternative Docs (ReDoc): `http://localhost:8000/redoc`

### 4. Frontend Setup

```bash
# Open a new terminal
# Navigate to frontend directory
cd frontend

# Install dependencies
npm install

# Start development server
npm run dev
```

Frontend will be available at `http://localhost:5173`

## 🗄️ Database Schema

### Core Tables
- **users**: Authentication and user credentials
- **student_profiles**: Student personal information and metadata
- **academic_terms**: Semester-wise academic records
- **subjects**: Individual subject scores and grades

### Key Relationships
- One-to-One: `users` ↔ `student_profiles`
- One-to-Many: `student_profiles` → `academic_terms`
- One-to-Many: `academic_terms` → `subjects`

## 🔐 Authentication Flow

### Login Process
1. Student enters `student_id` and password on login page
2. Frontend sends credentials to `/api/auth/login`
3. Backend verifies credentials and returns JWT access token
4. Token stored in localStorage and included in subsequent API requests
5. Protected routes require valid JWT token

### Password Format
- Default pattern: `{student_id}@123`
- Example: For student ID `2021001`, password is `2021001@123`
- Passwords are stored as bcrypt hashes in the database

## 📊 Data Import & Management

> **STALE:** CSV import scripts and academic-record imports no longer exist. Seed data comes from `backend/scripts/` (taxonomy, job requirements, learning resources).

### CSV Import Scripts

#### Import Student Profiles
```bash
cd backend
python scripts/import_students_from_csv.py
```

#### Import Academic Records
```bash
python scripts/import_academic_records.py
```

#### Generate/Refresh Passwords
```bash
python scripts/generate_passwords_bcrypt.py
```

### CSV File Format
- Place CSV files in `backend/data/` directory
- Expected columns documented in `backend/CSV_INTEGRATION.md`

## 🧪 Testing

### Backend Tests
```bash
cd backend
pytest tests/ -v
pytest --cov=app tests/  # With coverage
```

### API Testing
```bash
# Test individual endpoints
python test_api_endpoints.py

# Simulate student login
python simulate_login.py
```

### Frontend (Not yet implemented)

> **STALE:** The frontend is implemented (React, Vite, TypeScript); this note is out of date.
```bash
cd frontend
npm run test
```

## 📁 Project Structure

```
fullstack/
├── backend/
│   ├── alembic/              # Database migrations
│   ├── app/
│   │   ├── api/              # API routes
│   │   │   └── routes/       # Endpoint definitions
│   │   ├── core/             # Core configuration
│   │   ├── models/           # SQLAlchemy models
│   │   ├── schemas/          # Pydantic schemas
│   │   ├── services/         # Business logic
│   │   ├── middleware/       # Custom middleware
│   │   └── main.py           # Application entry point
│   ├── scripts/              # Utility scripts
│   ├── tests/                # Test suite
│   ├── data/                 # CSV data files
│   ├── requirements.txt      # Python dependencies
│   └── .env.example          # Environment template
├── frontend/
│   ├── src/
│   │   ├── api/              # API client & types
│   │   ├── components/       # React components
│   │   │   ├── common/       # Reusable components
│   │   │   ├── dashboard/    # Dashboard widgets
│   │   │   └── layout/       # Layout components
│   │   ├── pages/            # Route pages
│   │   ├── contexts/         # React contexts
│   │   ├── utils/            # Helper functions
│   │   ├── App.tsx           # Root component
│   │   └── main.tsx          # Entry point
│   ├── package.json          # Node dependencies
│   └── vite.config.ts        # Vite configuration
└── docker-compose.yml        # Docker setup

```

## 🔧 Configuration

### Backend Environment Variables
```env
# Database
DATABASE_URL=postgresql://studentadmin:studentpass123@localhost:5432/student_tracker
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10

# JWT Security
SECRET_KEY=your-secret-key-change-this-in-production
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# Application
APP_NAME=Student Academic Tracker
DEBUG=false
ENVIRONMENT=development

# CORS
CORS_ORIGINS=http://localhost:3000,http://localhost:5173
```

### Frontend Configuration
- API base URL configured in `src/api/client.ts`
- Default: `http://localhost:8000/api`

## 🌐 API Endpoints

### Authentication
- `POST /api/auth/login` - Student login with credentials
- `POST /api/auth/refresh` - Refresh access token
- `GET /api/auth/me` - Get current user info

### Profile

> **STALE:** these endpoints do not exist in the current backend.
- `GET /api/profile` - Get student profile
- `PUT /api/profile` - Update student profile

### Academic Records

> **STALE:** these endpoints do not exist in the current backend.
- `GET /api/academic/terms` - Get all academic terms
- `GET /api/academic/terms/{term_id}` - Get specific term
- `GET /api/academic/subjects` - Get all subjects

### Analytics

> **STALE:** these endpoints do not exist in the current backend.
- `GET /api/analytics/gpa-trend` - GPA trends over semesters
- `GET /api/analytics/subject-performance` - Subject-wise analytics
- `GET /api/analytics/weak-strong-subjects` - Performance analysis

### Health
- `GET /api/health` - API health check
- `GET /api/health/db` - Database connection status

## 🎨 UI Components

### Key Features
- **Modern Design**: Clean, professional interface with soft shadows and gradients
- **Card-based Layout**: Organized information in intuitive card components
- **Responsive Charts**: Interactive visualizations using Recharts
- **Loading States**: Skeleton screens and loading animations
- **Dark Mode**: System-wide theme toggle with localStorage persistence
- **Micro-interactions**: Smooth transitions and hover effects

## 🚧 Known Issues & Limitations

See `IMPROVEMENTS.md` for detailed list of planned enhancements and known issues.

### Current Limitations
- No real-time notifications
- Limited batch operations
- Basic error logging
- No data export functionality

## 📝 Development Workflow

### Creating Database Migrations
```bash
cd backend
alembic revision --autogenerate -m "Description of changes"
alembic upgrade head
```

### Adding New Features
1. Create/update models in `backend/app/models/`
2. Generate and run migrations
3. Create Pydantic schemas in `backend/app/schemas/`
4. Implement business logic in `backend/app/services/`
5. Add API routes in `backend/app/api/routes/`
6. Create frontend components in `frontend/src/components/`
7. Update types in `frontend/src/api/types.ts`
8. Connect to API in relevant pages

## 🤝 Contributing

### Code Style
- **Backend**: Follow PEP 8, use type hints
- **Frontend**: Follow ESLint rules, use TypeScript strict mode
- **Commits**: Use conventional commit messages

### Pull Request Process
1. Create feature branch from `main`
2. Implement changes with tests
3. Update documentation
4. Submit PR with detailed description

## 📚 Additional Documentation

- `backend/README.md` - Backend-specific documentation
- `backend/CSV_INTEGRATION.md` - CSV import guide
- `frontend/README.md` - Frontend-specific documentation
- `IMPROVEMENTS.md` - Planned features and enhancements

## 🐛 Troubleshooting

### Database Connection Issues
```bash
# Check if PostgreSQL is running
docker ps  # If using Docker
# or
pg_isready -h localhost -p 5432

# Verify credentials in .env match docker-compose.yml
```

### Migration Errors
```bash
# Reset database (WARNING: Deletes all data)
alembic downgrade base
alembic upgrade head

# Or manually drop and recreate
dropdb student_tracker
createdb student_tracker
alembic upgrade head
```

### Frontend Build Errors
```bash
# Clear node_modules and reinstall
rm -rf node_modules package-lock.json
npm install

# Clear Vite cache
rm -rf node_modules/.vite
```

### Password Authentication Issues
```bash
# Regenerate all student passwords
cd backend
python scripts/generate_passwords_bcrypt.py
```

## 📄 License

This project is proprietary software developed for educational institutions.

## 👥 Authors

Developed as part of PR17-Lab academic project.

---

**Last Updated**: January 2026  
**Version**: 1.0.0  
**Status**: Active Development