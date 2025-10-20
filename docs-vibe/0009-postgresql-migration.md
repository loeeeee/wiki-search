# PostgreSQL Migration

## Overview

The wiki-search project has been migrated from SQLite to PostgreSQL to provide better scalability, concurrent access, and production-ready database capabilities.

## Why PostgreSQL?

- **Scalability**: PostgreSQL handles large datasets more efficiently than SQLite
- **Concurrency**: Better support for multiple concurrent connections
- **Production Ready**: More suitable for production deployments
- **Advanced Features**: Better indexing, full-text search, and performance optimization
- **Remote Access**: Can be accessed from multiple clients simultaneously

## Configuration Changes

### 1. Dependencies

Added `psycopg[binary]>=3.2.0` to `pyproject.toml` for PostgreSQL connectivity.

### 2. Database Settings

Updated `wiki_search/wiki_search/settings.py` to use PostgreSQL with environment variables:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'wiki_search'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
        'HOST': os.environ.get('POSTGRES_HOST', '172.22.0.133'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}
```

### 3. Database-Specific Code Updates

#### clean_db.py
- Made database-agnostic by detecting backend using `connection.vendor`
- SQLite: Uses `VACUUM` for optimization
- PostgreSQL: Uses `VACUUM ANALYZE` for optimization
- Updated help text to be database-agnostic

#### load_wiki_dump.py
- Updated help text from "Load Wikipedia dump into SQLite" to "Load Wikipedia dump into database"

## Environment Variable Setup

### Required Variables

```bash
POSTGRES_DB=wiki_search          # Database name
POSTGRES_USER=your_username      # Database user
POSTGRES_PASSWORD=your_password  # Database password
POSTGRES_HOST=172.22.0.133       # Database host
POSTGRES_PORT=5432               # Database port
```

### Setup Instructions

1. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with actual credentials

3. Load environment variables:
   ```bash
   set -a; source .env; set +a
   ```

## Migration Steps

1. **Install dependencies:**
   ```bash
   uv sync
   ```

2. **Run migrations:**
   ```bash
   python wiki_search/manage.py migrate
   ```

3. **Verify connection:**
   ```bash
   python wiki_search/manage.py db_summary
   ```

4. **Load data:**
   ```bash
   python wiki_search/manage.py load_wiki_dump --workers 6
   ```

## Differences in Behavior

### Performance
- PostgreSQL may have different performance characteristics for bulk operations
- Connection pooling is handled differently
- Indexing strategies may need adjustment for optimal performance

### Database Optimization
- SQLite: `VACUUM` command
- PostgreSQL: `VACUUM ANALYZE` command

### Error Handling
- PostgreSQL provides more detailed error messages
- Connection timeouts are configurable via `connect_timeout` option

## Testing Connection

Before running migrations, test the PostgreSQL connection:

```bash
psql -h 172.22.0.133 -U your_username -d wiki_search
```

## Troubleshooting

### Common Issues

1. **Connection refused**: Check if PostgreSQL server is running and accessible
2. **Authentication failed**: Verify username and password in environment variables
3. **Database does not exist**: Ensure the database exists on the PostgreSQL server
4. **Permission denied**: Check user permissions for the database

### Debug Commands

```bash
# Test connection
python wiki_search/manage.py dbshell

# Check database status
python wiki_search/manage.py db_summary

# View current settings
python wiki_search/manage.py diffsettings
```
