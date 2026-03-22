# TradingWeb - Command Line Guide

A comprehensive guide to all commands needed for developing, running, and maintaining the TradingWeb fullstack application.

## 📋 Prerequisites

Before starting, ensure you have:
- **Docker & Docker Compose** installed
- **Git** for version control
- **Node.js 22.12.0+** (for local frontend development)
- **Python 3.10+** (for local backend development)

## 🚀 Quick Start

### 1. Clone and Setup
```bash
# Clone the repository
git clone <repository-url>
cd tradingweb

# Copy environment file and configure
cp .env.example .env
# Edit .env with your settings (database, secrets, etc.)
```

### 2. Launch Full Stack
```bash
# Start all services (backend, frontend, database, redis, celery)
docker compose up -d

# View logs from key services
docker compose logs -f t_backend t_frontend t_celery_beat

# Check service health
docker compose ps
```

### 3. Access Application
- **Frontend**: http://localhost:4321
- **Backend API**: http://localhost:8000
- **Django Admin**: http://localhost:8000/admin/

## 🛠️ Development Workflow

### Backend Development

#### Local Development (Recommended for frequent changes)
```bash
# Navigate to backend
cd backend

# Activate virtual environment
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py makemigrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Run development server
python manage.py runserver

# Run with auto-reload
python manage.py runserver --noreload
```

#### Docker Development
```bash
# Run Django commands in container
docker compose exec t_backend python manage.py makemigrations
docker compose exec t_backend python manage.py migrate
docker compose exec t_backend python manage.py createsuperuser

# Access Django shell
docker compose exec t_backend python manage.py shell

# Run tests
docker compose exec t_backend python manage.py test
```

### Frontend Development

#### Local Development
```bash
# Navigate to frontend
cd frontend

# Install dependencies
npm install

# Start development server
npm run dev

# Run quality checks
npm run check      # Astro type checking
npm run lint       # ESLint
npm run format     # Prettier formatting
npm run typecheck  # TypeScript checking
```

#### Docker Development
```bash
# Frontend runs automatically with docker compose up
# View frontend logs
docker compose logs -f t_frontend

# Rebuild frontend after config changes
docker compose build t_frontend
```

## 🗄️ Database Operations

### PostgreSQL Database
```bash
# Access database directly
docker compose exec t_db psql -U haradhansharma -d tradingview_db

# Backup database
docker compose exec t_db pg_dump -U haradhansharma tradingview_db > backup.sql

# Restore database
docker compose exec -T t_db psql -U haradhansharma tradingview_db < backup.sql

# Reset database (CAUTION: destroys all data)
docker compose down
docker volume rm tradingweb_t_postgres_data
docker compose up -d t_db
```

### Django Database Commands
```bash
# Create migrations
docker compose exec t_backend python manage.py makemigrations

# Apply migrations
docker compose exec t_backend python manage.py migrate

# Show migration status
docker compose exec t_backend python manage.py showmigrations

# Create superuser
docker compose exec t_backend python manage.py createsuperuser

# Flush database (remove all data)
docker compose exec t_backend python manage.py flush
```

## 🔄 Celery & Background Tasks

### Celery Worker
```bash
# Start worker (usually runs automatically)
docker compose up -d t_celery

# View worker logs
docker compose logs -f t_celery

# Restart worker
docker compose restart t_celery
```

### Celery Beat (Scheduler)
```bash
# Start scheduler (usually runs automatically)
docker compose up -d t_celery_beat

# View scheduler logs
docker compose logs -f t_celery_beat

# Check scheduled tasks in Django admin
# Go to: http://localhost:8000/admin/django_celery_beat/
```

### Redis (Message Broker)
```bash
# Access Redis CLI
docker compose exec t_redis redis-cli

# Check Redis info
docker compose exec t_redis redis-cli info

# Clear Redis data
docker compose exec t_redis redis-cli flushall
```

## 📊 Monitoring & Logs

### View All Logs
```bash
# All services
docker compose logs -f

# Specific services
docker compose logs -f t_backend t_frontend t_celery_beat

# Last 100 lines
docker compose logs --tail=100 t_backend

# Follow logs with timestamps
docker compose logs -f -t t_backend
```

### Service Status
```bash
# Check all services
docker compose ps

# Check specific service
docker compose ps t_backend

# Resource usage
docker stats
```

### Health Checks
```bash
# Django system checks
docker compose exec t_backend python manage.py check

# Database connectivity
docker compose exec t_backend python manage.py dbshell --command="SELECT 1;"

# Redis connectivity
docker compose exec t_redis redis-cli ping
```

## 🔧 Maintenance & Troubleshooting

### Rebuild Services
```bash
# Rebuild specific service
docker compose build t_backend
docker compose up -d t_backend

# Rebuild all services
docker compose build
docker compose up -d

# Force rebuild (no cache)
docker compose build --no-cache
```

### Clean Up
```bash
# Stop all services
docker compose down

# Remove containers and volumes (CAUTION: destroys data)
docker compose down -v

# Remove images
docker compose down --rmi all

# Clean up unused Docker resources
docker system prune -a --volumes
```

### Reset Everything
```bash
# Complete reset (CAUTION: destroys all data)
docker compose down -v --rmi all
docker volume prune -f
docker system prune -a -f

# Fresh start
docker compose up -d --build
```

### Common Issues

#### Backend Won't Start
```bash
# Check logs
docker compose logs t_backend

# Check database connection
docker compose exec t_backend python manage.py dbshell

# Rebuild backend
docker compose build t_backend
docker compose up -d t_backend
```

#### Frontend Won't Start
```bash
# Check logs
docker compose logs t_frontend

# Clear node_modules and rebuild
docker compose exec t_frontend rm -rf node_modules package-lock.json
docker compose exec t_frontend npm install
docker compose restart t_frontend
```

#### Database Connection Issues
```bash
# Check database logs
docker compose logs t_db

# Test connection
docker compose exec t_db psql -U haradhansharma -d tradingview_db -c "SELECT version();"

# Reset database
docker compose down
docker volume rm tradingweb_t_postgres_data
docker compose up -d t_db
```

## 🚀 Deployment

### Production Build
```bash
# Build for production
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Or set environment variable
export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
docker compose up -d --build
```

### Environment Configuration
```bash
# Development
cp .env.example .env.dev

# Production
cp .env.example .env.prod
# Edit with production values
```

## 📝 Useful Aliases (Optional)

Add these to your `~/.bashrc` or `~/.zshrc`:

```bash
# TradingWeb aliases
alias tw='cd /path/to/tradingweb'
alias twup='docker compose up -d'
alias twdown='docker compose down'
alias twlogs='docker compose logs -f'
alias twbuild='docker compose build'
alias twrestart='docker compose restart'
alias twshell='docker compose exec t_backend python manage.py shell'
alias twmigrate='docker compose exec t_backend python manage.py migrate'
```

## 🎯 Quick Reference

| Command | Description |
|---------|-------------|
| `docker compose up -d` | Start all services |
| `docker compose logs -f` | Follow all logs |
| `docker compose ps` | Show service status |
| `docker compose down` | Stop all services |
| `docker compose build` | Rebuild services |
| `docker compose exec t_backend python manage.py shell` | Django shell |
| `docker compose exec t_db psql -U user -d db` | Database shell |

## 📞 Support

If you encounter issues:
1. Check logs: `docker compose logs service_name`
2. Verify environment variables in `.env`
3. Ensure ports aren't in use: `netstat -tulpn \| grep :8000`
4. Try rebuilding: `docker compose build --no-cache`

For more help, check the individual service documentation or create an issue in the repository.